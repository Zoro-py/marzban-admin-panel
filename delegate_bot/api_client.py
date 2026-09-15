"""Thin async client over this project's /api/delegate/bot/* endpoints.

Deliberately NOT the same client as bot/api_client.py (which logs in with
the Marzban admin credentials and can reach every endpoint in the panel) or
shopbot/api_client.py (a different key for a different, public surface).
This one carries a single scoped key that opens nothing but delegate
self-service — not the ledger, not Marzban admin, not the shop.

This process takes messages from a trusted but still-external customer, so
holding only this narrow key bounds what a compromise of THIS process (or a
leaked DELEGATE_BOT_API_KEY) can reach — same reasoning as
backend/app/routers/delegate.py's module docstring.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class DelegateApiError(Exception):
    """Carries a message the backend intended for the END USER (a 400 from
    a DelegateError — over the credit limit, over the daily cap, not their
    account). Handlers show str(exc) to the delegate directly, so nothing
    internal should ever be raised as one of these."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class DelegateBackendClient:
    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Delegate-Bot-Key": api_key}

    async def _request(self, method: str, path: str, *, timeout: float = 20, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, f"{self._base_url}{path}", headers=self._headers, **kwargs)

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            return resp.text
        return detail if isinstance(detail, str) else str(detail)

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        resp = await self._request("GET", path, params=params)
        if resp.status_code >= 400:
            raise DelegateApiError(self._detail(resp), resp.status_code)
        return resp.json()

    async def post(self, path: str, json: Optional[dict] = None, timeout: float = 20) -> Any:
        resp = await self._request("POST", path, json=json, timeout=timeout)
        if resp.status_code >= 400:
            raise DelegateApiError(self._detail(resp), resp.status_code)
        return resp.json()


# Defaults rather than KeyError at import — same reasoning as shopbot's own
# client: the container is started by compose whether or not delegates are
# configured, and an unconfigured one has to reach main()'s idle branch
# instead of dying on the import line.
backend = DelegateBackendClient(
    base_url=os.environ.get("API_BASE_URL", "http://backend:8000"),
    api_key=os.environ.get("DELEGATE_BOT_API_KEY", ""),
)
