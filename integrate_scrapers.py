"""
Integration script for Walmart and Superstore scrapers.
Searches for multiple products across both stores and returns results.
"""

import asyncio
import json
import re
from datetime import datetime
from typing import List, Dict, Any, Set, Optional, Tuple

# Import the scraper functions
from walmart2 import scrape_walmart_cole_harbour
from superstore import scrape_superstore


# Products to search for
PRODUCTS_TO_SEARCH = [
    "Orange Juice",
    "Toilet Paper",
    "Lemons"
]


# --- Unit price extraction & normalization ---
UNIT_CONVERSIONS = {
    # Volume: to ml
    "ml": 1,
    "l": 1000,
    "L": 1000,
    "oz": 29.5735,
    "fl oz": 29.5735,
    # Weight: to g
    "g": 1,
    "kg": 1000,
    "lb": 453.592,
    "lbs": 453.592,
    # Count
    "ea": 1,
    "count": 1,
    "ct": 1,
    "piece": 1,
    "roll": 1,
    "rolls": 1,
}

PRODUCT_TYPE_KEYWORDS = {
    "milk|juice|beverage|drink|liquid|water|coffee|tea|soda": ("volume", "100ml"),
    "meat|turkey|chicken|beef|pork|deli|ham|bacon|sausage|fish": ("weight", "100g"),
    "apple|orange|banana|fruit|produce|vegetable": ("weight", "lb"),
    "bread|baked|cake|cookie|donut": ("weight", "100g"),
    "egg|eggs": ("count", "1ea"),
    "paper|tissue|toilet|towel|napkin": ("count", "1roll"),
    "cereal|pasta|rice|grain|flour": ("weight", "100g"),
}


def _parse_quantity_and_unit(quantity_str: Optional[str]) -> Optional[Tuple[float, str]]:
    """Extract amount and unit from quantity string like '2 L' or '12 Count'."""
    if not quantity_str or not isinstance(quantity_str, str):
        return None
    
    # Pattern: number (with optional spaces) followed by unit
    match = re.search(r'(\d+\.?\d*)\s*([a-zA-Z\s]+?)(?:\s*,|$)', quantity_str.strip())
    if match:
        try:
            amount = float(match.group(1))
            unit = match.group(2).strip().lower()
            return (amount, unit)
        except (ValueError, AttributeError):
            pass
    return None


def _extract_explicit_unit_price(quantity_str: Optional[str]) -> Optional[Tuple[float, str]]:
    """Extract explicit unit price like '$0.86/100ml' from quantity string."""
    if not quantity_str or not isinstance(quantity_str, str):
        return None
    
    # Pattern: $number/number+unit
    match = re.search(r'\$(\d+\.?\d*)/(\d+\.?\d*)\s*([a-zA-Z\s]+)', quantity_str.lower())
    if match:
        try:
            price = float(match.group(1))
            per_amount = float(match.group(2))
            per_unit = match.group(3).strip()
            # Normalize to "per 1 unit" for consistency
            if per_amount > 0:
                normalized_price = price / per_amount
                return (normalized_price, per_unit)
        except (ValueError, ZeroDivisionError):
            pass
    return None


def _calculate_unit_price(total_price: Optional[float], quantity_tuple: Optional[Tuple[float, str]]) -> Optional[Dict[str, Any]]:
    """Calculate unit price given total price and quantity."""
    if not total_price or not quantity_tuple:
        return None
    
    amount, unit = quantity_tuple
    if amount <= 0:
        return None
    
    # Normalize unit to base unit
    unit_lower = unit.lower()
    if unit_lower not in UNIT_CONVERSIONS:
        # Try fuzzy match
        for key in UNIT_CONVERSIONS:
            if key in unit_lower or unit_lower in key:
                unit_lower = key
                break
        else:
            # Unknown unit, return count-based
            price_per_unit = total_price / amount
            return {"amount": round(price_per_unit, 4), "per": f"1{unit}", "base_unit": unit_lower}
    
    # Calculate price per base unit
    base_unit_conversion = UNIT_CONVERSIONS[unit_lower]
    total_base_units = amount * base_unit_conversion
    price_per_base = total_price / total_base_units if total_base_units > 0 else None
    
    if price_per_base is None:
        return None
    
    # Return both raw and normalized
    return {
        "amount": round(total_price / amount, 4),
        "per": f"{amount}{unit}",
        "normalized_amount": round(price_per_base, 4),
        "normalized_per": f"1{unit_lower}",
        "base_unit": unit_lower
    }


def _detect_product_type(product_name: str) -> str:
    """Detect product type from name."""
    name_lower = product_name.lower()
    for keywords, (ptype, display_unit) in PRODUCT_TYPE_KEYWORDS.items():
        if re.search(keywords, name_lower):
            return ptype
    return "unknown"


def _get_best_display_unit(product_type: str) -> str:
    """Get best display unit for a product type."""
    for keywords, (ptype, display_unit) in PRODUCT_TYPE_KEYWORDS.items():
        if ptype == product_type:
            return display_unit
    return "1unit"


def _enrich_unit_prices(item: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich an item with calculated unit prices."""
    item_copy = item.copy()
    
    price = item_copy.get("price")
    quantity = item_copy.get("quantity")
    
    # Try to extract explicit unit price from quantity field first
    explicit = _extract_explicit_unit_price(quantity)
    if explicit:
        explicit_price, explicit_unit = explicit
        item_copy["unit_price_display"] = f"${explicit_price:.2f}/{explicit_unit}"
        item_copy["unit_prices"] = [{"amount": explicit_price, "per": explicit_unit}]
        return item_copy
    
    # Parse quantity and calculate
    qty_tuple = _parse_quantity_and_unit(quantity)
    if qty_tuple and price is not None:
        calc = _calculate_unit_price(price, qty_tuple)
        if calc:
            item_copy["unit_prices"] = [calc]
            # Choose display format based on product type
            product_type = _detect_product_type(item_copy.get("name", ""))
            display_unit = _get_best_display_unit(product_type)
            
            # Calculate price for display unit
            if "normalized_amount" in calc:
                base = calc["normalized_amount"]
                if "100ml" in display_unit or "100g" in display_unit:
                    display_price = base * 100
                    item_copy["unit_price_display"] = f"${display_price:.2f}/{display_unit}"
                elif "lb" in display_unit:
                    # Convert g to lb if needed
                    if calc["base_unit"] == "g":
                        display_price = base * 453.592
                        item_copy["unit_price_display"] = f"${display_price:.2f}/{display_unit}"
                    else:
                        item_copy["unit_price_display"] = f"${base:.2f}/{display_unit}"
                elif "1ea" in display_unit or "1roll" in display_unit:
                    item_copy["unit_price_display"] = f"${base:.2f}/{display_unit}"
                else:
                    item_copy["unit_price_display"] = f"${base:.4f}/unit"
            else:
                item_copy["unit_price_display"] = f"${calc['amount']:.2f}/{calc['per']}"
    
    return item_copy


# --- Relevance filtering (no category data available) ---
NEGATIVE_TERMS = {
    "toy",
    "easter",
    "decor",
    "costume",
    "gift card",
    "giftcard",
    "digital",
    "ebook",
    "ornament",
}


def _tokenize(text: str) -> Set[str]:
    return {t for t in text.lower().replace("%", "% ").split() if t}


def _is_relevant(query: str, item: Dict[str, Any]) -> bool:
    # Require at least 50% of query tokens to appear in the combined text
    q_tokens = _tokenize(query)
    name = item.get("name") or ""
    quantity = item.get("quantity") or ""
    unit_price = item.get("unit_price") or ""
    combined = f"{name} {quantity} {unit_price}".lower()

    # Negative term gate
    if any(neg in combined for neg in NEGATIVE_TERMS):
        return False

    # Match at least 50% of tokens (or minimum 2, whichever is lower)
    match_count = sum(1 for token in q_tokens if token in combined)
    min_required = max(2, len(q_tokens) // 2)
    return match_count >= min_required


def _filter_and_rank(query: str, items: List[Dict[str, Any]], limit: int = 10) -> List[Dict[str, Any]]:
    filtered = [itm for itm in items if _is_relevant(query, itm)]
    # Prefer items with a numeric price present
    filtered.sort(key=lambda x: (x.get("price") is None, x.get("price", 0)))
    # Enrich with unit prices
    enriched = [_enrich_unit_prices(itm) for itm in filtered[:limit]]
    return enriched


def _find_cheapest_option(product_name: str, stores_dict: Dict[str, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """
    Find the cheapest option across all stores for a product.
    Prioritizes unit price comparison when available, falls back to total price.
    
    Returns:
        {"store": "Walmart", "item": {...}} or None if no valid items
    """
    all_items = []
    
    # Collect all items from all stores with store labels
    for store_name, items in stores_dict.items():
        for item in items:
            if item.get("price") is not None:  # Only consider items with prices
                all_items.append({
                    "store": store_name,
                    "item": item
                })
    
    if not all_items:
        return None
    
    # Try to find cheapest by normalized unit price first (fairest comparison)
    items_with_normalized = []
    for entry in all_items:
        unit_prices = entry["item"].get("unit_prices", [])
        if unit_prices and isinstance(unit_prices, list) and len(unit_prices) > 0:
            normalized = unit_prices[0].get("normalized_amount")
            if normalized is not None:
                items_with_normalized.append({
                    "store": entry["store"],
                    "item": entry["item"],
                    "normalized_price": normalized
                })
    
    # If we have items with normalized unit prices, use those for comparison
    if items_with_normalized:
        # Prefer available items, then by normalized unit price
        items_with_normalized.sort(key=lambda x: (
            not x["item"].get("available", False),  # Available items first
            x["normalized_price"]
        ))
        best = items_with_normalized[0]
        return {"store": best["store"], "item": best["item"], "comparison_type": "unit_price"}
    
    # Fall back to total price comparison
    all_items.sort(key=lambda x: (
        not x["item"].get("available", False),  # Available items first
        x["item"].get("price", float('inf'))
    ))
    
    return {"store": all_items[0]["store"], "item": all_items[0]["item"], "comparison_type": "total_price"}


async def search_all_products() -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Search for all products across both stores.
    
    Returns:
        {
            "product_name": {
                "walmart": [...items],
                "superstore": [...items]
            },
            ...
        }
    """
    results = {}
    
    for product in PRODUCTS_TO_SEARCH:
        print(f"\n{'='*80}")
        print(f"Searching for: {product}")
        print(f"{'='*80}")
        
        results[product] = {
            "walmart": [],
            "superstore": []
        }
        
        # Search Superstore
        print(f"\n[SUPERSTORE] Searching for '{product}'...")
        try:
            superstore_items = await scrape_superstore(product)
            filtered_superstore = _filter_and_rank(product, superstore_items)
            results[product]["superstore"] = filtered_superstore
            print(f"[SUPERSTORE] Found {len(superstore_items)} items | kept {len(filtered_superstore)} relevant")
        except Exception as e:
            print(f"[SUPERSTORE] Error: {e}")
            results[product]["superstore"] = []
        
        # Search Walmart
        print(f"\n[WALMART] Searching for '{product}'...")
        try:
            walmart_items = await scrape_walmart_cole_harbour(product)
            filtered_walmart = _filter_and_rank(product, walmart_items)
            results[product]["walmart"] = filtered_walmart
            print(f"[WALMART] Found {len(walmart_items)} items | kept {len(filtered_walmart)} relevant")
        except Exception as e:
            print(f"[WALMART] Error (may require manual captcha): {type(e).__name__}: {e}")
            results[product]["walmart"] = []
        
        await asyncio.sleep(2)  # Small delay between searches
    
    return results


def print_comparison(results: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> None:
    """Pretty print the comparison results."""
    
    print(f"\n\n{'='*100}")
    print(f"COMPARISON RESULTS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*100}\n")
    
    for product, stores in results.items():
        print(f"\n{'─'*100}")
        print(f"PRODUCT: {product.upper()}")
        print(f"{'─'*100}")
        
        for store_name, items in stores.items():
            print(f"\n{store_name.upper()} - {len(items)} items found:")
            print("─" * 100)
            
            if not items:
                print("  (No items found)")
                continue
            
            for idx, item in enumerate(items[:5], 1):  # Show top 5
                name = item.get("name", "Unknown")
                price = item.get("price", "N/A")
                unit_price_display = item.get("unit_price_display", "N/A")
                quantity = item.get("quantity", "N/A")
                available = "✓" if item.get("available") else "✗"
                
                if isinstance(price, (int, float)):
                    price_str = f"${price:.2f}"
                else:
                    price_str = str(price) if price else "N/A"
                
                unit_str = f" | Unit: {unit_price_display}" if unit_price_display and unit_price_display != "N/A" else ""
                qty_str = f" | Qty: {quantity}" if quantity and quantity != "N/A" else ""
                
                print(f"  {idx}. {name}")
                print(f"     Price: {price_str}{unit_str}{qty_str} | Available: {available}")
        
        # Show best deal for this product
        print(f"\n{'═'*100}")
        best_deal = _find_cheapest_option(product, stores)
        if best_deal:
            store = best_deal["store"].upper()
            item = best_deal["item"]
            comp_type = best_deal.get("comparison_type", "total_price")
            
            name = item.get("name", "Unknown")
            price = item.get("price", "N/A")
            unit_price_display = item.get("unit_price_display", "N/A")
            quantity = item.get("quantity", "N/A")
            available = "✓" if item.get("available") else "✗"
            
            if isinstance(price, (int, float)):
                price_str = f"${price:.2f}"
            else:
                price_str = str(price)
            
            unit_str = f" | Unit: {unit_price_display}" if unit_price_display and unit_price_display != "N/A" else ""
            qty_str = f" | Qty: {quantity}" if quantity and quantity != "N/A" else ""
            
            comparison_note = " (by unit price)" if comp_type == "unit_price" else " (by total price)"
            print(f"🏆 BEST DEAL{comparison_note}: {store} - \"{name}\"")
            print(f"   Price: {price_str}{unit_str}{qty_str} | Available: {available}")
        else:
            print(f"🏆 BEST DEAL: No items with valid prices found")
        print(f"{'═'*100}")
        
        print()


def save_results(results: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> None:
    """Save results to JSON file."""
    output_file = f"comparison_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\nResults saved to: {output_file}")


async def main():
    """Main entry point."""
    print("Starting integrated scraper...")
    print(f"Products to search: {PRODUCTS_TO_SEARCH}")
    print(f"Stores: Walmart (Cole Harbour), Real Canadian Superstore")
    
    try:
        results = await search_all_products()
        print_comparison(results)
        save_results(results)
        
    except KeyboardInterrupt:
        print("\n\nScraping interrupted by user.")
    except Exception as e:
        print(f"\n\nFatal error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
