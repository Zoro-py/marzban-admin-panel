"""Thin async client over this project's /api/shop/bot/* endpoints.

Deliberately NOT the same client as bot/api_client.py. That one logs in with
the Marzban admin credentials and can reach every endpoint in the panel; this
one carries a single scoped key that opens nothing but the shop's own
customer-facing surface.

This process takes messages from the general public, so it is the most likely
thing here to be compromised. Keeping the admin credentials out of it is what
bounds what a compromise costs — see the module docstring in
backend/app/routers/shop.py.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class ShopApiError(Exception):
    """Carries a message the backend intended for the END USER (a 400 from a
    ShopError — insufficient balance, shop closed, volume out of range).
    Handlers show `str(exc)` to the customer directly, so nothing internal
    should ever be raised as one of these."""


class ShopBackendClient:
    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Shop-Bot-Key": api_key}

    async def _request(self, method: str, path: str, *, timeout: float = 20, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, f"{self._base_url}{path}", headers=self._headers, **kwargs)

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            return resp.text
        # FastAPI's 422 puts a LIST under "detail"; str() keeps it printable
        # rather than leaking a raw Python repr into a customer's chat.
        return detail if isinstance(detail, str) else str(detail)

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        resp = await self._request("GET", path, params=params)
        if resp.status_code >= 400:
            raise ShopApiError(self._detail(resp))
        return resp.json()

    async def post(self, path: str, json: Optional[dict] = None, timeout: float = 20) -> Any:
        resp = await self._request("POST", path, json=json, timeout=timeout)
        if resp.status_code >= 400:
            raise ShopApiError(self._detail(resp))
        return resp.json()


backend = ShopBackendClient(
    base_url=os.environ["API_BASE_URL"],
    api_key=os.environ["SHOP_BOT_API_KEY"],
)
