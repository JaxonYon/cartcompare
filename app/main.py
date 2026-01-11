from fastapi import FastAPI
from pydantic import BaseModel
from typing import List

app = FastAPI(title="Smart Cart API")

class ItemsRequest(BaseModel):
    items: List[str]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/compare")
async def compare(req: ItemsRequest):
    # Placeholder: will call scrapers and matching logic in later iterations.
    # For now, echo the requested items back.
    return {"status": "queued", "items": req.items}
