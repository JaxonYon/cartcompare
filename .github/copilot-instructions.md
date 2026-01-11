# Copilot Instructions for Walmart Web Scraper

## Project Overview
This is a web scraper for Walmart Canada's Cole Harbour store that extracts product search results including pricing, availability, and unit pricing. Uses Playwright for browser automation with anti-detection stealth techniques to bypass captcha protection.

## Architecture & Key Components

### Core Scraping Pattern
The scraper (`scrape_walmart_cole_harbour`) follows this flow:
1. **Browser Setup**: Launches headless Chromium with specific user-agent and viewport
2. **Location Spoofing**: Injects cookies to force Cole Harbour store context (Store #1176) and postal code (B2V2J5) to prevent "Price Missing" errors
3. **Anti-Detection**: Applies `playwright_stealth` module to avoid bot detection
4. **Captcha Handling**: Implements manual solve requirement with polling detection (60 retries, 2s intervals)
5. **Data Extraction**: Targets `<script id="__NEXT_DATA__">` containing Next.js serialized JSON with search results
6. **JSON Parsing**: Defensively extracts data path: `props → pageProps → initialData → searchResult → itemStacks → items`

### Critical Implementation Details
- **Manual Intervention**: Scraper uses `headless=False` to allow user to solve captchas interactively
- **Fallback Parsing**: If JSON parsing fails, attempts substring extraction from `{` to `}` to recover partial data
- **Nullable Fields**: Price can come from `currentPrice.price` or fallback to `linePrice.price`; unit pricing is optional
- **Type Checking**: Validates `__typename == "Product"` and checks dict types before accessing nested properties
- **Clean Exit**: Always closes browser context even on errors via try/finally pattern

## Dependencies & Setup

### Required Packages
- `playwright`: Async browser automation
- `playwright-stealth`: Anti-bot detection module (critical - must install before running)
- Standard library: `json`, `asyncio`, `random`, `traceback`

### Installation
```bash
pip install playwright-stealth
```
Failure to install stealth results in graceful degradation (stealth_async becomes None).

## Code Patterns & Conventions

### Error Handling
- Uses nested try/except blocks with specific recovery for JSON parsing failures
- Logs errors to stdout with `traceback.print_exc()` for debugging
- Returns empty list `[]` on any fatal error to signal scrape failure
- Validates data types (dict, list) before nested access to prevent KeyError

### Async/Await Pattern
- All browser operations are async with `async_playwright()` context manager
- Single entry point: `asyncio.run(scrape_walmart_cole_harbour(search_term))`
- No background task management or task cleanup needed

### Output Format
Results are dictionaries with consistent schema:
```python
{
    "name": str,           # Product name (required)
    "price": float | None, # CAD price or None if missing
    "unit_price": str | None,  # Pre-formatted unit price string ("$X.XX/unit")
    "available": bool      # True if IN_STOCK status
}
```

## Testing & Debugging Workflow

### Local Execution
```bash
python walmart2.py
```
- Prompts browser window if captcha detected; watch for console message: "!!! ACTION REQUIRED..."
- Default test search: "great value milk" (modify `if __name__ == "__main__"` block)

### Common Issues
- **"Price Missing" in results**: Cookies not injected correctly; verify store ID 1176 and postal code B2V2J5
- **Data tag timeout**: Walmart detected bot; need to solve captcha faster or increase retry limit
- **JSON parse failure**: Website structure changed; check if `__NEXT_DATA__` script tag still contains full JSON

### Debugging Tips
- Use `headless=False` already enabled for visual inspection
- Increase `wait_until="domcontentloaded"` timeout if pages load slowly
- Modify the 60-retry loop and 2-second sleep for more/fewer captcha attempts
- Print `raw_json_str[:500]` to inspect JSON structure if parsing fails

## Extending the Scraper

### Adding New Data Fields
1. Check Next.js JSON path for new field in dev tools
2. Add to item iteration loop with safe type checking: `item.get("fieldName")`
3. Append new keys to results dictionary

### Changing Target Store
Update cookies in `context.add_cookies()`:
- `"walmart.id"`: Store number (currently 1176)
- `"locDataV3"`: Postal code (currently B2V2J5)

### Modifying Search Behavior
Search term URL encoding happens in: `search_term.replace(' ', '%20')` within the URL f-string.
