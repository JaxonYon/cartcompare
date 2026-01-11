# Smart Cart — Canadian Grocery Price Optimizer

Quickstart (development):

1. Create and activate a Python virtualenv:

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows
```

2. Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install
```

3. Run the API (development):

```bash
uvicorn app.main:app --reload --port 8000
```

Notes:
- Playwright scrapers open a real browser window by default (`headless=False`) so you can solve CAPTCHAs if encountered.
- The repo currently includes a Walmart scraper at `walmart2.py`. The API skeleton is minimal and will later call the scrapers.
