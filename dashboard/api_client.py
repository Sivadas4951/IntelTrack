"""
api_client.py
-------------
Thin client for the IntelTrack FastAPI layer. Views never touch the database,
so the dashboard shows exactly what the API (and therefore the evidence ledger) says.

get() returns (data, error). Exactly one of them is None.
"""

import json

import httpx
import streamlit as st

DEFAULT_API_URL = "http://localhost:8000"


@st.cache_data(ttl=5, show_spinner=False)
def _cached_get(base: str, path: str, params_json: str):
    try:
        r = httpx.get(f"{base}{path}", params=json.loads(params_json), timeout=20)
        if r.status_code >= 400:
            detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
            return None, f"{r.status_code}: {detail}"
        return r.json(), None
    except Exception as e:
        return None, f"Cannot reach API at {base} ({e})"


class Api:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def get(self, path: str, **params):
        clean = {k: v for k, v in params.items() if v is not None}
        return _cached_get(self.base, path, json.dumps(clean, sort_keys=True))

    def post(self, path: str, json_body: dict | None = None, **params):
        """POST is never cached; it clears the GET cache so the UI reflects the change."""
        try:
            r = httpx.post(f"{self.base}{path}", params=params or None, json=json_body, timeout=30)
            _cached_get.clear()
            if r.status_code >= 400:
                return None, f"{r.status_code}: {r.text}"
            return r.json(), None
        except Exception as e:
            return None, f"Cannot reach API at {self.base} ({e})"