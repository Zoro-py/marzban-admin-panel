"""Thin async client over this project's own backend API.

A deliberate copy of bot/api_client.py's BackendClient (the same shape
shopbot/api_client.py already re-ships): each bot process is its own trust
boundary with its own credentials, so they do not share a package. This one
holds the operator's Marzban login — the receipts bot can move real money
(approve/reject a top-up), so it authenticates exactly like the operator bot
does, not with a narrower key.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx


class BackendClient:
    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        resp = await client.post(
            f"{self._base_url}/api/auth/login",
            json={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        self._token_expires_at = time.monotonic() + 12 * 60 * 60
        return self._token

    async def _request(self, method: str, path: str, timeout: float = 20, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            token = await self._get_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)
            if resp.status_code == 401:
                self._token = None
                token = await self._get_token(client)
                headers["Authorization"] = f"Bearer {token}"
                resp = await client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)
            return resp

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        resp = await self._request("GET", path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, json: Optional[dict] = None, timeout: float = 20) -> Any:
        resp = await self._request("POST", path, json=json, timeout=timeout)
        if resp.status_code >= 400:
            # Surface the backend's own refusal sentence (e.g. "This top-up
            # was already approved.") — it is exactly what the operator needs.
            try:
                raise ValueError(resp.json().get("detail") or resp.text)
            except ValueError:
                raise
            except Exception:
                resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()


backend = BackendClient(
    os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"),
    os.environ.get("MARZBAN_USERNAME", ""),
    os.environ.get("MARZBAN_PASSWORD", ""),
)
