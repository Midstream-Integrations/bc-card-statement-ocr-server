"""API-key check for BC-facing endpoints (X-API-Key header = OCR_API_KEY)."""
import hmac

from fastapi import Header, HTTPException

from core.settings import API_KEY


def require_key(x_api_key: str = Header(default="")):
    if not API_KEY or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "Invalid API key")
