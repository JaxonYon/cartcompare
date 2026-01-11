"""Quick test to verify quantity extraction works."""
import asyncio
from superstore import scrape_superstore

async def test():
    print("Testing quantity extraction with Superstore...")
    results = await scrape_superstore("2% white milk")
    
    print(f"\nFound {len(results)} items\n")
    for idx, item in enumerate(results[:5], 1):
        print(f"{idx}. {item['name']}")
        print(f"   Price: ${item['price']:.2f} | Quantity: {item['quantity']}")
        print()

if __name__ == "__main__":
    asyncio.run(test())
