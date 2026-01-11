import json
import asyncio
import random
import traceback
import time
from playwright.async_api import async_playwright

# IMPORTANT: Run 'pip install playwright-stealth' before running this.
# v2.0.0+ exports just 'stealth' which works for both sync and async.
try:
    from playwright_stealth import stealth_async
except ImportError:
    try:
        from playwright_stealth import stealth as stealth_async  # v2.0.0+ API
    except ImportError:
        print("Please install playwright-stealth: pip install playwright-stealth")
        stealth_async = None


async def _safe_close(context=None, browser=None):
    """Close context/browser without raising if already gone."""
    for obj in (context, browser):
        try:
            if obj:
                await obj.close()
        except Exception:
            pass


# Rotate between multiple realistic user agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

def _get_realistic_headers():
    """Generate realistic HTTP headers."""
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


async def scrape_walmart_cole_harbour(search_term):
    context = None
    browser = None
    async with async_playwright() as p:
        selected_user_agent = random.choice(USER_AGENTS)
        
        # Launch browser with anti-detection settings
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )
        
        # Random viewport to avoid fingerprinting
        viewport_widths = [1366, 1920, 1440, 1024]
        viewport_height = 768
        selected_width = random.choice(viewport_widths)
        
        context = await browser.new_context(
            user_agent=selected_user_agent,
            viewport={'width': selected_width, 'height': viewport_height},
            device_scale_factor=1,
            extra_http_headers=_get_realistic_headers(),
            ignore_https_errors=True,
        )
        
        # Add anti-detection cookies
        await context.add_cookies([
            {
                "name": "walmart.id",
                "value": "1176",
                "domain": ".walmart.ca",
                "path": "/"
            },
            {
                "name": "locDataV3",
                "value": "B2V2J5",
                "domain": ".walmart.ca",
                "path": "/"
            }
        ])
        
        page = await context.new_page()
        
        # Inject stealth scripts before any navigation
        try:
            if stealth_async:
                await stealth_async(page)
        except Exception:
            pass
        
        # Inject additional anti-bot scripts
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false,
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
        """)

        def _extract_quantity(item: dict):
            """Extract package size/quantity information."""
            import re
            # Try several paths for quantity/size info
            for key in ("packageSizing", "size", "quantity", "format", "packaging", "unitQuantity", "volumePrice"):
                val = item.get(key)
                if val and isinstance(val, str) and val.strip():
                    return val.strip()
            
            # Try extracting from name as fallback
            name = item.get("name", "")
            if name:
                # Look for common patterns: "2L", "1L", "6 pack", "12 count", etc.
                match = re.search(r'(\d+\s*(?:L|ML|g|kg|oz|lb|pack|count|piece))', name, re.IGNORECASE)
                if match:
                    return match.group(1)
            
            return None

        def _extract_price_and_unit(item: dict):
            """Try several known paths to extract a numeric price and unit price string."""
            price = None
            unit_price = None

            # Primary: priceInfo variations
            price_info = item.get("priceInfo") or {}
            if isinstance(price_info, dict):
                # currentPrice may be dict or primitive
                curr = price_info.get("currentPrice") or price_info.get("price")
                if isinstance(curr, dict):
                    price = curr.get("price") or curr.get("value") or curr.get("priceString")
                elif curr is not None:
                    price = curr

                if price is None:
                    line = price_info.get("linePrice")
                    if isinstance(line, dict):
                        price = line.get("price") or line.get("value") or line.get("priceString")

                unit = price_info.get("unitPrice")
                if isinstance(unit, dict):
                    unit_price = unit.get("priceString") or unit.get("price") or unit.get("value")

            # Secondary: offers / offer(s)
            if price is None:
                offers = item.get("offers") or item.get("offer")
                if isinstance(offers, dict):
                    price = offers.get("price") or offers.get("priceString") or offers.get("amount")
                elif isinstance(offers, list) and offers:
                    first = offers[0]
                    if isinstance(first, dict):
                        price = first.get("price") or first.get("priceString") or first.get("amount")

            # Tertiary: other common keys
            for candidate in ("productPrice", "sellingPrice", "price", "salePrice"):
                if price is None:
                    v = item.get(candidate)
                    if isinstance(v, dict):
                        price = v.get("price") or v.get("value") or v.get("priceString")
                    elif v is not None:
                        price = v

            # Normalize price: unwrap dicts, strip currency, convert to float when possible
            if isinstance(price, dict):
                price = price.get("price") or price.get("value") or price.get("amount")

            if isinstance(price, str):
                p = price.strip()
                # remove common currency symbols
                for s in ("$", "CAD", "USD"):
                    p = p.replace(s, "").strip()
                try:
                    if p.replace(',', '').replace('.', '').isdigit():
                        price = float(p.replace(',', ''))
                except Exception:
                    pass

            return price, unit_price

        def _extract_availability(item: dict):
            # Combine several possible flags to determine availability.
            # 1) Explicit negative flag
            if item.get("isOutOfStock") is True:
                return False

            # 2) Explicit add-to-cart / buy flags — treat these as positive availability
            if item.get("canAddToCart") is True or item.get("showAtc") is True or item.get("showBuyNow") is True:
                return True

            # 3) Status strings and boolean flags
            status = item.get("availabilityStatus")
            if isinstance(status, str) and status.upper() in ("IN_STOCK", "INSTOCK", "AVAILABLE", "IN_STORE", "IN_STORE_ONLY"):
                return True
            if item.get("isInStock") is True or item.get("isAvailable") is True:
                return True

            # 4) Inventory counts
            inv = item.get("inventory") or item.get("inventoryInfo") or item.get("inStoreAvailability")
            if isinstance(inv, dict):
                for key in ("availableQuantity", "quantity", "stock", "available"):
                    v = inv.get(key)
                    try:
                        if isinstance(v, (int, float)) and v > 0:
                            return True
                        if isinstance(v, str) and v.isdigit() and int(v) > 0:
                            return True
                    except Exception:
                        pass

            # 5) Offers / fulfillment
            offers = item.get("offers") or item.get("offer")
            if isinstance(offers, dict):
                av = offers.get("availability") or offers.get("availabilityStatus")
                if isinstance(av, str) and av.upper() in ("IN_STOCK", "INSTOCK", "AVAILABLE", "IN_STOCK_ONLINE"):
                    return True
                if offers.get("isAvailable") is True:
                    return True
            elif isinstance(offers, list) and offers:
                first = offers[0]
                if isinstance(first, dict):
                    av = first.get("availability") or first.get("availabilityStatus")
                    if isinstance(av, str) and av.upper() in ("IN_STOCK", "INSTOCK", "AVAILABLE"):
                        return True
                    if first.get("isAvailable") is True:
                        return True

            fulfil = item.get("fulfillment") or item.get("fulfillmentInfo") or item.get("fulfillmentOptions")
            if isinstance(fulfil, dict):
                if fulfil.get("isAvailable") is True or fulfil.get("isFulfillable") is True:
                    return True

            # 6) Textual availability messages
            avail_msg = item.get("availabilityMessage") or item.get("availabilityText") or item.get("availability")
            if isinstance(avail_msg, str) and any(kw in avail_msg.lower() for kw in ("in stock", "available", "add to cart", "available online")):
                return True

            # 7) If we have price/offer and no explicit out-of-stock, be permissive
            out_flags = ("OUT_OF_STOCK", "SOLD_OUT", "UNAVAILABLE", "COMING_SOON")
            if (item.get("priceInfo") or item.get("price") or item.get("offers")) and not (isinstance(status, str) and status.upper() in out_flags):
                return True

            # Default: not available
            return False

        def _debug_item_details(item: dict):
            """Print a concise snapshot of selected fields for debugging.

            Avoid dumping huge blobs; show primitive values, dict keys, and list lengths.
            """
            def short(v, maxlen=160):
                try:
                    if v is None:
                        return "None"
                    if isinstance(v, (int, float, bool)):
                        return str(v)
                    if isinstance(v, str):
                        s = v.strip()
                        return (s[:maxlen] + "...") if len(s) > maxlen else s
                    if isinstance(v, dict):
                        keys = list(v.keys())
                        return f"dict(keys={keys[:8]})"
                    if isinstance(v, list):
                        if not v:
                            return "list(len=0)"
                        first = v[0]
                        if isinstance(first, dict):
                            return f"list(len={len(v)}, first=dict(keys={list(first.keys())[:6]}))"
                        return f"list(len={len(v)}, first_type={type(first).__name__})"
                    return repr(v)[:maxlen]
                except Exception:
                    return "<error>"

            keys = [
                "sku", "id", "productId", "name",
                "availabilityStatus", "isInStock", "isAvailable",
                "priceInfo", "price", "offers", "inventory", "fulfillment",
                "availabilityMessage", "availabilityText"
            ]

            identifier = item.get("sku") or item.get("productId") or item.get("id") or item.get("name")
            pieces = [f"DebugItem id={identifier!s}"]
            for k in keys:
                if k in item:
                    pieces.append(f"{k}={short(item.get(k))}")

            print(" | ".join(pieces))

        url = f"https://www.walmart.ca/search?q={search_term.replace(' ', '%20')}"
        
        try:
            print(f"Navigating to Walmart (Store #1176 - Cole Harbour)...")
            print(f"Using user agent: {selected_user_agent[:60]}...")
            
            # Add realistic delay before navigation
            await asyncio.sleep(random.uniform(1, 3))
            
            # Using 'domcontentloaded' to ensure we see the captcha early
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            
            # Add more realistic delays after page load
            await asyncio.sleep(random.uniform(2, 4))
            
            # --- CAPTCHA DETECTION & WAIT ---
            for attempt in range(120):  # Increase timeout to 2 minutes
                try:
                    content = await page.content()
                except Exception:
                    await asyncio.sleep(1)
                    continue
                    
                content_lower = content.lower()
                
                is_blocked = any(phrase in content_lower for phrase in [
                    "press & hold", 
                    "verify you are human",
                    "robot or human",
                    "access to this page has been denied",
                    "unusual traffic from your computer",
                    "we detected unusual traffic",
                ])
                
                has_data = await page.locator("script#__NEXT_DATA__").count() > 0
                
                if is_blocked and not has_data:
                    if attempt % 10 == 0:
                        print(f"!!! ACTION REQUIRED: Solve the captcha in the browser window !!! (attempt {attempt})")
                    # Randomize wait time to avoid detection
                    await asyncio.sleep(random.uniform(1.5, 3))
                elif has_data:
                    print("Data tag detected! Proceeding with extraction...")
                    break
                else:
                    # Random micro-delays to seem more human
                    await asyncio.sleep(random.uniform(0.5, 1.5))

            script_locator = page.locator("script#__NEXT_DATA__")
            
            try:
                await script_locator.wait_for(state="attached", timeout=20000)
            except Exception as e:
                print(f"Timeout: Data tag not found. Error: {e}")
                # Try one more refresh before giving up
                print("Attempting page refresh...")
                await asyncio.sleep(2)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                    if await page.locator("script#__NEXT_DATA__").count() == 0:
                        await _safe_close(context, browser)
                        return []
                except Exception:
                    await _safe_close(context, browser)
                    return []

            raw_json_str = await script_locator.inner_text()

            data = None
            try:
                data = json.loads(raw_json_str)
            except Exception:
                start = raw_json_str.find('{')
                end = raw_json_str.rfind('}')
                if start != -1 and end != -1:
                    try:
                        data = json.loads(raw_json_str[start:end+1])
                    except: 
                        data = None

            if not isinstance(data, dict):
                print(f"Error: Parsed data is {type(data)}.")
                await _safe_close(context, browser)
                return []

            try:
                props = data.get('props', {})
                page_props = props.get('pageProps', {})
                initial_data = page_props.get('initialData', {})
                search_result = initial_data.get('searchResult', {})
                item_stacks = search_result.get('itemStacks', [])
                
                if not item_stacks:
                    print("No product stacks found.")
                    await _safe_close(context, browser)
                    return []

                items = []
                for stack in item_stacks:
                    items.extend(stack.get('items', []))
                    
            except Exception as e:
                print(f"JSON structure error: {e}")
                await _safe_close(context, browser)
                return []
            
            results = []
            debug_items = []
            for item in items:
                if not isinstance(item, dict) or item.get("__typename") != "Product":
                    continue
                
                name = item.get("name")
                price, unit_price = _extract_price_and_unit(item)
                available = _extract_availability(item)
                quantity = _extract_quantity(item)

                # If price still missing, emit a concise debug line to help diagnose structure
                identifier = item.get("sku") or item.get("productId") or item.get("id") or name
                if price is None:
                    print(f"Debug: missing price for {identifier!s}; keys: {sorted(list(item.keys()))}")

                results.append({
                    "name": name,
                    "price": price,
                    "unit_price": unit_price,
                    "quantity": quantity,
                    "available": available
                })
                # Collect a small debug summary (no large payloads)
                try:
                    debug_items.append({
                        "identifier": identifier,
                        "name": name,
                        "keys": sorted(list(item.keys())),
                        "price": price,
                        "unit_price": unit_price,
                        "availabilityStatus": item.get("availabilityStatus"),
                        "offers_present": isinstance(item.get("offers") or item.get("offer"), (dict, list)),
                        "inventory_present": bool(item.get("inventory") or item.get("inventoryInfo") or item.get("inStoreAvailability")),
                        "availabilityMessage": item.get("availabilityMessage") or item.get("availabilityText") or item.get("availability")
                    })
                except Exception:
                    pass

            print(f"Successfully retrieved {len(results)} items.")
            # Write debug summaries to a file for inspection
            try:
                with open("debug_items.json", "w", encoding="utf-8") as df:
                    json.dump(debug_items, df, indent=2, ensure_ascii=False)
                print("Wrote per-item debug summary to debug_items.json")
            except Exception as e:
                print(f"Failed to write debug file: {e}")
            await _safe_close(context, browser)
            return results

        except Exception as e:
            print(f"Scraper Error: {e}")
            traceback.print_exc()
            await _safe_close(context, browser)
            return []

if __name__ == "__main__":
    search_query = "great value milk"
    res = asyncio.run(scrape_walmart_cole_harbour(search_query))
    if res:
        print(f"\nResults for '{search_query}' at Cole Harbour:")
        for r in res[:10]: 
            p = f"${r['price']}" if r['price'] is not None else "Price Missing"
            u = r['unit_price'] if r['unit_price'] else "N/A"
            status = "In Stock" if r['available'] else "Unavailable"
            print(f"- {r['name']}\n  Price: {p} | Unit: {u} | Status: {status}\n")