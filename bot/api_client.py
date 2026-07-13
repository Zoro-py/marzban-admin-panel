"""Thin async client over this project's own backend API (not Marzban directly —
the backend is the single source of truth for ownership/ledger, so the bot and the
web dashboard always see the same state)."""

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

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=20) as client:
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

    async def post(self, path: str, json: Optional[dict] = None) -> Any:
        resp = await self._request("POST", path, json=json)
        if resp.status_code >= 400:
            raise ValueError(resp.json().get("detail", resp.text))
        return resp.json()

    async def patch(self, path: str, json: Optional[dict] = None) -> Any:
        resp = await self._request("PATCH", path, json=json)
        if resp.status_code >= 400:
            raise ValueError(resp.json().get("detail", resp.text))
        return resp.json()


backend = BackendClient(
    base_url=os.environ["API_BASE_URL"],
    username=os.environ["MARZBAN_USERNAME"],
    password=os.environ["MARZBAN_PASSWORD"],
)
