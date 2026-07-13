"""Thin async client over the Marzban REST API.

Only wraps the handful of endpoints this backend actually needs: login,
listing users (for sync), reading one user, creating a user, and modifying
a user (used for the "extend/reduce time" live action). Everything else
about a Marzban user (proxies/inbounds shape) is passed through as-is
instead of guessed, since that depends on how this specific panel's
inbounds are configured.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from app.config import settings


class MarzbanAuthError(RuntimeError):
    pass


class MarzbanUnavailable(RuntimeError):
    """Marzban couldn't be reached at all (network/DNS/timeout/bad base URL) —
    distinct from MarzbanAuthError (reachable, credentials rejected) and from a
    4xx/5xx application error (reachable, request itself was invalid)."""

    pass


class MarzbanClient:
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
            f"{self._base_url}/api/admin/token",
            data={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise MarzbanAuthError(f"Marzban login failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        self._token = data["access_token"]
        # Marzban tokens are long-lived; refresh proactively every 30 min regardless.
        self._token_expires_at = time.monotonic() + 30 * 60
        return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                token = await self._get_token(client)
                headers = kwargs.pop("headers", {})
                headers["Authorization"] = f"Bearer {token}"
                resp = await client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)

                if resp.status_code == 401:
                    # Token invalidated server-side; force one retry with a fresh login.
                    self._token = None
                    token = await self._get_token(client)
                    headers["Authorization"] = f"Bearer {token}"
                    resp = await client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)

                return resp
        except MarzbanAuthError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise MarzbanUnavailable(f"Could not reach Marzban at {self._base_url}: {exc}") from exc

    async def list_users(self, offset: int = 0, limit: int = 200) -> dict:
        resp = await self._request("GET", "/api/users", params={"offset": offset, "limit": limit})
        resp.raise_for_status()
        return resp.json()

    async def get_user(self, username: str) -> Optional[dict]:
        resp = await self._request("GET", f"/api/user/{username}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def create_user(self, payload: dict) -> dict:
        resp = await self._request("POST", "/api/user", json=payload)
        if resp.status_code >= 400:
            raise ValueError(f"Marzban create_user failed ({resp.status_code}): {resp.text}")
        return resp.json()

    async def modify_user(self, username: str, payload: dict) -> dict:
        resp = await self._request("PUT", f"/api/user/{username}", json=payload)
        if resp.status_code >= 400:
            raise ValueError(f"Marzban modify_user failed ({resp.status_code}): {resp.text}")
        return resp.json()

    async def verify_admin_login(self, username: str, password: str) -> bool:
        """One-off credential check against Marzban's own /api/admin/token, used to
        gate this dashboard's login — deliberately independent of the cached
        service-account token above, so it neither reads nor clobbers it."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{self._base_url}/api/admin/token",
                    data={"username": username, "password": password},
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise MarzbanUnavailable(f"Could not reach Marzban at {self._base_url}: {exc}") from exc
        return resp.status_code == 200


marzban_client = MarzbanClient(
    base_url=settings.marzban_base_url,
    username=settings.marzban_username,
    password=settings.marzban_password,
)
