"""Test enhanced Walmart scraper with anti-bot improvements."""
import asyncio
from walmart2 import scrape_walmart_cole_harbour

async def test_walmart():
    print("Testing enhanced Walmart scraper with improved anti-bot measures...\n")
    results = await scrape_walmart_cole_harbour("2% milk")
    
    print(f"\nFound {len(results)} items\n")
    for idx, item in enumerate(results[:5], 1):
        name = item.get("name", "Unknown")
        price = item.get("price", "N/A")
        qty = item.get("quantity", "N/A")
        avail = "✓" if item.get("available") else "✗"
        
        price_str = f"${price:.2f}" if isinstance(price, (int, float)) else str(price)
        print(f"{idx}. {name}")
        print(f"   Price: {price_str} | Size: {qty} | Available: {avail}\n")

if __name__ == "__main__":
    asyncio.run(test_walmart())
