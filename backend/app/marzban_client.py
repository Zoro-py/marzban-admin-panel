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
from jose import jwt as jose_jwt

from app.config import settings

# Refresh this long before the token's real expiry (from its own `exp` claim),
# not exactly at it — clock skew and a request already in flight near the
# deadline shouldn't turn into an avoidable 401.
REFRESH_SAFETY_MARGIN_SECONDS = 5 * 60
# Only used if the token can't be decoded or carries no exp claim at all —
# Marzban's tokens are normally long-lived (hours to weeks depending on
# install), so this is a conservative floor, not the expected path.
FALLBACK_CACHE_SECONDS = 30 * 60


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
        self._token_expires_at = self._compute_cache_deadline(self._token)
        return self._token

    @staticmethod
    def _compute_cache_deadline(token: str) -> float:
        """Reads the token's own `exp` claim (wall-clock unix timestamp) and
        converts it to a `time.monotonic()` deadline, so a login every sync
        cycle only happens when the token is actually about to expire — not
        on a fixed guessed interval shorter than its real lifetime, which was
        forcing a fresh Marzban login (and its notification) on every sync run
        regardless of how long the token was actually still good for."""
        try:
            claims = jose_jwt.get_unverified_claims(token)
            exp = claims.get("exp")
            if not exp:
                return time.monotonic() + FALLBACK_CACHE_SECONDS
            seconds_until_real_expiry = float(exp) - time.time()
            useful_seconds = seconds_until_real_expiry - REFRESH_SAFETY_MARGIN_SECONDS
            return time.monotonic() + max(0.0, useful_seconds)
        except Exception:
            return time.monotonic() + FALLBACK_CACHE_SECONDS

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

    async def reset_user(self, username: str) -> dict:
        """Resets used_traffic to 0 for a new cycle. Marzban does not reset
        lifetime_used_traffic here — that field is specifically the monotonic
        counter our own usage_baseline billing math depends on."""
        resp = await self._request("POST", f"/api/user/{username}/reset")
        if resp.status_code >= 400:
            raise ValueError(f"Marzban reset_user failed ({resp.status_code}): {resp.text}")
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
