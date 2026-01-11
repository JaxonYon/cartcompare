import asyncio
import json
import re
import traceback
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from playwright.async_api import async_playwright

# IMPORTANT: playwright-stealth v2.0.0+ exports just 'stealth' which works for both sync/async.
try:
    from playwright_stealth import stealth_async
except ImportError:
    try:
        from playwright_stealth import stealth as stealth_async  # v2.0.0+ API
    except ImportError:
        print("Please install playwright-stealth: pip install playwright-stealth")
        stealth_async = None

# Default location: Cole Harbour, NS (postal code aligned with the Walmart scraper)
DEFAULT_POSTAL_CODE = "B2V2J5"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _strip_currency(val: str) -> Optional[float]:
    cleaned = re.sub(r"[^0-9.,]", "", val)
    cleaned = cleaned.replace(",", "").strip()
    try:
        return float(cleaned)
    except Exception:
        return None


def _normalize_price(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return _strip_currency(v)
    if isinstance(v, dict):
        for key in (
            "sale",
            "current",
            "list",
            "regular",
            "price",
            "amount",
            "value",
            "priceValue",
        ):
            if key in v:
                maybe = _normalize_price(v.get(key))
                if maybe is not None:
                    return maybe
    return None


def _extract_unit_price(v: Any) -> Optional[str]:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for key in ("unitPrice", "unit", "pricePerUnit", "comparisonPrice"):
            if key in v:
                inner = v.get(key)
                if isinstance(inner, str):
                    return inner.strip()
    return None


def _extract_price_fields(item: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    price: Optional[float] = None
    unit_price: Optional[str] = None

    # Common loblaws/PCX shapes: pricing -> price or prices
    pricing = item.get("pricing") or item.get("price") or item.get("prices")
    if isinstance(pricing, dict):
        for key in (
            "price",
            "current",
            "regular",
            "sale",
            "list",
            "value",
            "priceValue",
        ):
            if price is None and key in pricing:
                price = _normalize_price(pricing.get(key))
        if unit_price is None:
            unit_price = _extract_unit_price(pricing)

    # Sometimes price lives directly as primitive/dict
    if price is None and "price" in item:
        price = _normalize_price(item.get("price"))

    if price is None and "regularPrice" in item:
        price = _normalize_price(item.get("regularPrice"))

    if unit_price is None:
        unit_price = _extract_unit_price(item)

    return price, unit_price


def _is_available(item: Dict[str, Any]) -> bool:
    availability_fields = (
        item.get("availabilityStatus"),
        item.get("availability"),
        item.get("availabilityMessage"),
        item.get("availabilityText"),
    )
    for val in availability_fields:
        if isinstance(val, str) and any(
            kw in val.lower()
            for kw in ("in stock", "available", "add to cart", "available online")
        ):
            return True

    flags = (
        item.get("isAvailable"),
        item.get("available"),
        item.get("buyable"),
        item.get("canAddToCart"),
    )
    if any(flag is True for flag in flags):
        return True

    # If we have a price and no explicit out-of-stock markers, lean optimistic
    price, _ = _extract_price_fields(item)
    status = item.get("availabilityStatus")
    if price is not None and not (
        isinstance(status, str)
        and status.upper() in ("OUT_OF_STOCK", "SOLD_OUT", "UNAVAILABLE")
    ):
        return True
    return False


def _extract_quantity(product: Dict[str, Any]) -> Optional[str]:
    """Extract package size/quantity from product dict."""
    # Try common field names
    for key in ("packageSizing", "size", "quantity", "format", "packaging", "unitQuantity", "volumePrice"):
        val = product.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    
    # Try extracting from name/title
    name = product.get("title") or product.get("name") or ""
    if name:
        match = re.search(r'(\d+\s*(?:L|ML|g|kg|oz|lb|pack|count|piece|ct))', name, re.IGNORECASE)
        if match:
            return match.group(1)
    
    return None


def _collect_products(tree: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []

    def walk(node: Any):
        if isinstance(node, dict):
            looks_like_product = False
            if node.get("__typename") in ("Product", "SellableProduct"):
                looks_like_product = True
            if isinstance(node.get("name"), str) and any(
                key in node for key in ("price", "pricing", "prices", "regularPrice")
            ):
                looks_like_product = True
            if isinstance(node.get("title"), str) and any(
                key in node for key in ("price", "pricing", "prices", "regularPrice")
            ):
                looks_like_product = True
            if looks_like_product:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tree)
    return found


def _unique_identifier(item: Dict[str, Any]) -> str:
    for key in ("sku", "id", "productId", "code", "gtin", "upc"):
        if key in item and item.get(key):
            return str(item.get(key))
    name = item.get("name") or item.get("title") or "unknown"
    return str(name)


async def scrape_superstore(search_term: str, postal_code: str = DEFAULT_POSTAL_CODE) -> List[Dict[str, Any]]:
    """
    Scrape Real Canadian Superstore search results using Playwright.

    Returns a list of dicts with schema:
    {"name": str, "price": float|None, "unit_price": str|None, "available": bool}
    """

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
            extra_http_headers={
                "Accept-Language": "en-CA,en;q=0.9",
                "Referer": "https://www.realcanadiansuperstore.ca/",
            },
        )

        # Hint the site about postal code; site also prompts in-page if it needs confirmation.
        await context.add_init_script(
            f"""
            try {{
                window.localStorage.setItem('pcx:postal_code','{postal_code}');
                window.localStorage.setItem('pcx:preferred_store_postal','{postal_code}');
            }} catch (e) {{}}
            """
        )

        page = await context.new_page()
        try:
            if stealth_async:
                await stealth_async(page)
        except Exception:
            pass

        search_url = f"https://www.realcanadiansuperstore.ca/search?search-bar={quote_plus(search_term)}"
        try:
            print("Navigating to Superstore search page...")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)

            # Basic captcha/blocked detection loop
            for i in range(60):
                content = (await page.content()).lower()
                blocked = any(
                    phrase in content
                    for phrase in (
                        "verify you are human",
                        "press & hold",
                        "access to this page has been denied",
                        "unusual traffic",
                    )
                )
                has_data = await page.locator("script#__NEXT_DATA__").count() > 0
                if blocked and not has_data:
                    if i % 5 == 0:
                        print("!!! ACTION REQUIRED: Solve any captcha in the browser window !!!")
                    await asyncio.sleep(2)
                elif has_data:
                    break
                else:
                    await asyncio.sleep(1)

            script = page.locator("script#__NEXT_DATA__")
            try:
                await script.wait_for(state="attached", timeout=20000)
            except Exception:
                print("Timeout: __NEXT_DATA__ script not found.")
                await browser.close()
                return []

            raw_json = await script.inner_text()
            try:
                with open("debug_superstore_raw.json", "w", encoding="utf-8") as rf:
                    rf.write(raw_json[:50000])
                print(f"Saved raw __NEXT_DATA__ snippet (len={len(raw_json)}) to debug_superstore_raw.json")
            except Exception as e:
                print(f"Failed to write raw debug: {e}")
            data: Any = None
            try:
                data = json.loads(raw_json)
            except Exception:
                start = raw_json.find("{")
                end = raw_json.rfind("}")
                if start != -1 and end != -1:
                    try:
                        data = json.loads(raw_json[start : end + 1])
                    except Exception:
                        data = None

            if not isinstance(data, dict):
                print("Error: could not parse Next.js data blob.")
                await browser.close()
                return []

            # Basic structure probes for debugging
            try:
                top_keys = list(data.keys()) if isinstance(data, dict) else []
                props = data.get("props", {}) if isinstance(data, dict) else {}
                page_props = props.get("pageProps", {}) if isinstance(props, dict) else {}
                print(f"Top-level keys: {top_keys}")
                print(f"props keys: {list(props.keys()) if isinstance(props, dict) else []}")
                print(f"pageProps keys: {list(page_props.keys()) if isinstance(page_props, dict) else []}")

                initial_search = page_props.get("initialSearchData") if isinstance(page_props, dict) else None
                if initial_search is not None:
                    try:
                        with open("debug_superstore_initial.json", "w", encoding="utf-8") as sf:
                            json.dump(initial_search, sf, indent=2, ensure_ascii=False)
                        print("Wrote debug_superstore_initial.json")
                    except Exception as e:
                        print(f"Failed to write initialSearchData: {e}")
            except Exception:
                pass

            products = _collect_products(data)
            if not products:
                print("Warning: no product-like objects found in parsed data.")

            results: List[Dict[str, Any]] = []
            debug: List[Dict[str, Any]] = []
            seen: set[str] = set()

            for item in products:
                if not isinstance(item, dict):
                    continue

                name = item.get("name") or item.get("title")
                if not isinstance(name, str):
                    continue

                pid = _unique_identifier(item)
                if pid in seen:
                    continue
                seen.add(pid)

                price, unit_price = _extract_price_fields(item)
                available = _is_available(item)
                quantity = _extract_quantity(item)

                results.append(
                    {
                        "name": name,
                        "price": price,
                        "unit_price": unit_price,
                        "quantity": quantity,
                        "available": available,
                    }
                )

                debug.append(
                    {
                        "id": pid,
                        "name": name,
                        "has_price": price is not None,
                        "unit_price": unit_price,
                        "availabilityStatus": item.get("availabilityStatus"),
                        "keys": sorted(list(item.keys())),
                    }
                )

            print(f"Superstore: extracted {len(results)} items (unique by id/name).")
            try:
                with open("debug_superstore.json", "w", encoding="utf-8") as df:
                    json.dump(debug, df, indent=2, ensure_ascii=False)
                print("Wrote debug_superstore.json for inspection.")
            except Exception as e:
                print(f"Failed to write debug file: {e}")

            await browser.close()
            return results

        except Exception as exc:
            print(f"Superstore scraper error: {exc}")
            traceback.print_exc()
            await browser.close()
            return []


if __name__ == "__main__":
    query = "milk"
    output = asyncio.run(scrape_superstore(query))
    print(f"Found {len(output)} items for '{query}'. Showing first 10...")
    for entry in output[:10]:
        p = entry["price"]
        u = entry["unit_price"] or "N/A"
        status = "In Stock" if entry["available"] else "Unavailable"
        price_str = f"${p}" if p is not None else "Price Missing"
        print(f"- {entry['name']} | {price_str} | Unit: {u} | {status}")
