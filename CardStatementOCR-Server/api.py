"""
BC-facing REST API. nginx routes https://<host>/api/* here.
Each statement type plugs in its own router; add new ones below.
"""
from fastapi import FastAPI

from cards import store as cards_store
from cards.api import router as cards_router
from reqapproval.api import router as reqapproval_router
from reqapproval import store as reqapproval_store

app = FastAPI(title="OCR API", docs_url=None, redoc_url=None, openapi_url=None)

cards_store.init()
reqapproval_store.init()

app.include_router(cards_router)
app.include_router(reqapproval_router)

@app.get("/api/health")
def health():
    return {"ok": True}
