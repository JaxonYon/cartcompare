import asyncio
import json
import re
import traceback
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
except ImportError:
    print("Please install playwright-stealth: pip install playwright-stealth")
    stealth_async = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Default postal code for Cole Harbour, NS
DEFAULT_POSTAL_CODE = "B2V2J5"


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
            "price",
            "salePrice",
            "regularPrice",
            "current",
            "amount",
            "value",
            "list",
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
        for key in ("unitPrice", "comparisonPrice", "pricePer", "pricePerUnit"):
            if key in v and isinstance(v.get(key), str):
                return v.get(key).strip()
    return None


def _extract_price_fields(item: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    price: Optional[float] = None
    unit_price: Optional[str] = None

    pricing = item.get("pricing") or item.get("price") or item.get("prices")
    if isinstance(pricing, dict):
        for key in (
            "price",
            "salePrice",
            "regularPrice",
            "current",
            "amount",
            "value",
            "list",
        ):
            if price is None and key in pricing:
                price = _normalize_price(pricing.get(key))
        if unit_price is None:
            unit_price = _extract_unit_price(pricing)

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
        item.get("inventoryStatus"),
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
        item.get("sellable"),
    )
    if any(flag is True for flag in flags):
        return True

    price, _ = _extract_price_fields(item)
    status = item.get("availabilityStatus") or item.get("inventoryStatus")
    if price is not None and not (
        isinstance(status, str)
        and status.upper() in ("OUT_OF_STOCK", "SOLD_OUT", "UNAVAILABLE")
    ):
        return True
    return False


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


async def scrape_sobeys(search_term: str, postal_code: str = DEFAULT_POSTAL_CODE) -> List[Dict[str, Any]]:
    """
    Best-effort Sobeys search scraper using Playwright.
    Returns list of {name, price, unit_price, available}.
    """

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
            extra_http_headers={
                "Accept-Language": "en-CA,en;q=0.9",
                "Referer": "https://www.sobeys.com/",
            },
        )

        page = await context.new_page()
        try:
            if stealth_async:
                await stealth_async(page)
        except Exception:
            pass

        # Navigate to Sobeys home first to potentially set location
        try:
            print("Loading Sobeys homepage to set location...")
            await page.goto("https://www.sobeys.com/", wait_until="domcontentloaded", timeout=45000)
            
            # Try to set location via localStorage
            await page.evaluate(f"""
                try {{
                    localStorage.setItem('postalCode', '{postal_code}');
                    localStorage.setItem('preferredPostal', '{postal_code}');
                    localStorage.setItem('sobeys_postal_code', '{postal_code}');
                }} catch(e) {{
                    console.log('Error setting localStorage:', e);
                }}
            """)
            
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Warning: Could not set store location: {e}")

        search_url = f"https://www.sobeys.com/?query={quote_plus(search_term)}&tab=products"
        try:
            print(f"Navigating to search page: {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)

            # Give page time to show modals
            print("Waiting for modals to appear...")
            await asyncio.sleep(3)
            
            # Handle cookie consent popup
            print("Looking for cookie consent popup...")
            try:
                # Try common cookie consent button selectors
                cookie_selectors = [
                    'button:has-text("Accept")',
                    'button:has-text("Accept All")',
                    'button:has-text("I Accept")',
                    'button:has-text("Agree")',
                    '[id*="accept"]',
                    '[class*="accept"]',
                    'button[aria-label*="Accept"]',
                ]
                for selector in cookie_selectors:
                    if await page.locator(selector).count() > 0:
                        print(f"Found cookie consent button: {selector}")
                        await page.locator(selector).first.click()
                        await asyncio.sleep(1)
                        break
            except Exception as e:
                print(f"Cookie consent handling: {e}")
            
            # Handle location popup
            print("Looking for location popup...")
            await asyncio.sleep(1)
            try:
                # Look for location input or skip button
                location_selectors = [
                    'input[placeholder*="postal"]',
                    'input[placeholder*="Postal"]',
                    'input[type="text"][name*="postal"]',
                ]
                
                location_input_found = False
                for selector in location_selectors:
                    if await page.locator(selector).count() > 0:
                        print(f"Found location input: {selector}")
                        await page.locator(selector).first.fill(postal_code)
                        await asyncio.sleep(1)
                        
                        # Try to submit
                        submit_selectors = [
                            'button:has-text("Submit")',
                            'button:has-text("Confirm")',
                            'button:has-text("Set")',
                            'button[type="submit"]',
                        ]
                        for submit_sel in submit_selectors:
                            if await page.locator(submit_sel).count() > 0:
                                await page.locator(submit_sel).first.click()
                                await asyncio.sleep(2)
                                break
                        location_input_found = True
                        break
                
                # Try to close/skip location modal
                if not location_input_found:
                    close_selectors = [
                        'button:has-text("Skip")',
                        'button:has-text("Close")',
                        'button:has-text("Not Now")',
                        '[aria-label*="Close"]',
                        '[class*="close"]',
                    ]
                    for selector in close_selectors:
                        if await page.locator(selector).count() > 0:
                            print(f"Closing location modal: {selector}")
                            await page.locator(selector).first.click()
                            await asyncio.sleep(1)
                            break
            except Exception as e:
                print(f"Location popup handling: {e}")

            # Give page extra time to render after modals
            print("Waiting for page to fully load...")
            await asyncio.sleep(3)
            
            # Check if page is still open
            if page.is_closed():
                print("Error: Page closed unexpectedly after navigation")
                await browser.close()
                return []
            
            print(f"Current URL: {page.url}")

            for i in range(60):
                try:
                    content = (await page.content()).lower()
                except Exception as e:
                    print(f"Error getting page content: {e}")
                    try:
                        html = await page.content()
                        with open("debug_sobeys_page.html", "w", encoding="utf-8") as hf:
                            hf.write(html)
                        print("Saved page HTML to debug_sobeys_page.html")
                    except:
                        pass
                    await browser.close()
                    return []
                    
                if "page you are looking for is not available" in content or "page not found" in content:
                    print("Sobeys returned a 'page not available' message.")
                    try:
                        html = await page.content()
                        with open("debug_sobeys_page.html", "w", encoding="utf-8") as hf:
                            hf.write(html)
                        print("Saved page HTML to debug_sobeys_page.html")
                    except Exception as e:
                        print(f"Failed to write debug HTML: {e}")
                    await browser.close()
                    return []

                blocked = any(
                    phrase in content
                    for phrase in (
                        "verify you are human",
                        "press & hold",
                        "access to this page has been denied",
                        "unusual traffic",
                    )
                )
                has_next = await page.locator("script#__NEXT_DATA__").count() > 0
                
                print(f"Loop {i}: has_next={has_next}, blocked={blocked}")
                
                if blocked and not has_next:
                    if i % 5 == 0:
                        print("!!! ACTION REQUIRED: Solve any captcha in the browser window !!!")
                    await asyncio.sleep(2)
                elif has_next:
                    print("Data tag detected! Proceeding with extraction...")
                    break
                elif i >= 5:  # After 5 attempts, break and try to extract anyway
                    print("No __NEXT_DATA__ found after waiting; attempting extraction anyway...")
                    break
                else:
                    await asyncio.sleep(1)

            script_data: Any = None
            raw_json = ""

            print("Looking for data scripts...")
            if await page.locator("script#__NEXT_DATA__").count() > 0:
                print("Found script#__NEXT_DATA__")
                script = page.locator("script#__NEXT_DATA__").first
                await script.wait_for(state="attached", timeout=20000)
                raw_json = await script.inner_text()
            else:
                print("No script#__NEXT_DATA__ found, trying fallback...")
                # Fallback: grab any big application/json script blobs
                scripts = page.locator('script[type="application/json"]')
                script_count = await scripts.count()
                print(f"Found {script_count} application/json scripts")
                if script_count > 0:
                    raw_json = await scripts.nth(0).inner_text()

            if raw_json:
                try:
                    with open("debug_sobeys_raw.json", "w", encoding="utf-8") as rf:
                        rf.write(raw_json[:100000])  # Save more data
                    print(f"Saved raw data snippet (len={len(raw_json)}) to debug_sobeys_raw.json")
                except Exception as e:
                    print(f"Failed to write raw debug: {e}")

            if raw_json:
                try:
                    script_data = json.loads(raw_json)
                except Exception:
                    start = raw_json.find("{")
                    end = raw_json.rfind("}")
                    if start != -1 and end != -1:
                        try:
                            script_data = json.loads(raw_json[start : end + 1])
                        except Exception:
                            script_data = None

            if script_data is None:
                try:
                    print("Trying window.__NEXT_DATA__...")
                    script_data = await page.evaluate("() => window.__NEXT_DATA__ || window.__INITIAL_STATE__ || null")
                    if script_data:
                        print(f"Found window data: {type(script_data)}")
                except Exception as e:
                    print(f"Error evaluating window data: {e}")
                    script_data = None

            if not isinstance(script_data, dict):
                print(f"Script data is not a dict, it's: {type(script_data)}")
                try:
                    html = await page.content()
                    with open("debug_sobeys_page.html", "w", encoding="utf-8") as hf:
                        hf.write(html)
                    print("Saved raw HTML to debug_sobeys_page.html for inspection.")
                except Exception:
                    pass
                print("Error: could not parse Sobeys data blob; returning empty list.")
                await browser.close()
                return []

            print(f"Script data keys: {list(script_data.keys()) if isinstance(script_data, dict) else 'N/A'}")

            try:
                props = script_data.get("props", {}) if isinstance(script_data, dict) else {}
                page_props = props.get("pageProps", {}) if isinstance(props, dict) else {}
                print(f"pageProps keys: {list(page_props.keys()) if isinstance(page_props, dict) else 'N/A'}")
                initial = page_props.get("initialSearchData") if isinstance(page_props, dict) else None
                if initial is not None:
                    with open("debug_sobeys_initial.json", "w", encoding="utf-8") as sf:
                        json.dump(initial, sf, indent=2, ensure_ascii=False)
                    print("Wrote debug_sobeys_initial.json")
                else:
                    print("No initialSearchData found")
            except Exception as e:
                print(f"Error extracting pageProps: {e}")

            products = _collect_products(script_data)
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

                results.append(
                    {
                        "name": name,
                        "price": price,
                        "unit_price": unit_price,
                        "available": available,
                    }
                )

                debug.append(
                    {
                        "id": pid,
                        "name": name,
                        "has_price": price is not None,
                        "unit_price": unit_price,
                        "availabilityStatus": item.get("availabilityStatus") or item.get("inventoryStatus"),
                        "keys": sorted(list(item.keys())),
                    }
                )

            print(f"Sobeys: extracted {len(results)} items (unique by id/name).")
            try:
                with open("debug_sobeys.json", "w", encoding="utf-8") as df:
                    json.dump(debug, df, indent=2, ensure_ascii=False)
                print("Wrote debug_sobeys.json for inspection.")
            except Exception as e:
                print(f"Failed to write debug file: {e}")

            await browser.close()
            return results

        except Exception as exc:
            print(f"Sobeys scraper error: {exc}")
            traceback.print_exc()
            await browser.close()
            return []


if __name__ == "__main__":
    query = "milk"
    output = asyncio.run(scrape_sobeys(query))
    print(f"\nFound {len(output)} items for '{query}'. Showing first 10...")
    for entry in output[:10]:
        p = entry["price"]
        u = entry["unit_price"] or "N/A"
        status = "In Stock" if entry["available"] else "Unavailable"
        price_str = f"${p}" if p is not None else "Price Missing"
        print(f"- {entry['name']} | {price_str} | Unit: {u} | {status}")
