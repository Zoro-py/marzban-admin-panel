"""Panel UI harness (U1-MOBILE, 2026-09-29 checklist area 2.2).

Serves the REAL backend + the built REAL frontend on one local port so
Playwright can sweep every page against a copy of the live database:

    python scripts/panel_harness.py --db backend/livecopy_0929.db --port 8031

Safety properties, in order of importance:
- The DB you pass is opened READ-ONLY-BY-COPY discipline: pass a COPY (the
  checklist's livecopy_0929.db), never the live file. The harness itself
  never writes money: the scheduler never starts (uvicorn lifespan="off",
  so sync/backup/settlement/nudge jobs don't exist), and Marzban is a
  recording fake — any endpoint that tries to reach the panel records the
  call and gets a benign answer instead.
- require_auth is overridden, but the real /api/auth/login still works
  (verify_admin_login is stubbed True) so the Login page is swept for real.

The frontend must be built first: (cd frontend && npm run build).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="path to a COPY of the sqlite DB (never the live file)")
    ap.add_argument("--port", type=int, default=8031)
    ap.add_argument("--dist", default=str(REPO / "frontend" / "dist"))
    args = ap.parse_args()

    db = Path(args.db).resolve()
    if not db.exists():
        sys.exit(f"no such db: {db}")
    dist = Path(args.dist).resolve()
    if not (dist / "index.html").exists():
        sys.exit(f"frontend not built at {dist} — run (cd frontend && npm run build)")

    # DATABASE_URL must be set BEFORE anything imports app.config.
    os.environ["DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
    os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
    os.environ.setdefault("MARZBAN_USERNAME", "harness")
    os.environ.setdefault("MARZBAN_PASSWORD", "harness")
    for k in ("BOT_TOKEN", "BOT_ADMIN_CHAT_ID", "SHOP_BOT_TOKEN", "SHOP_BOT_API_KEY",
              "DELEGATE_BOT_TOKEN", "DELEGATE_BOT_API_KEY", "NOTIFY_TELEGRAM_TOKEN"):
        os.environ[k] = ""

    sys.path.insert(0, str(REPO / "backend"))

    import uvicorn  # noqa: E402
    from fastapi.staticfiles import StaticFiles  # noqa: E402
    from fastapi.responses import FileResponse  # noqa: E402

    from app import marzban_client as marzban_module  # noqa: E402
    from app.auth import require_auth  # noqa: E402
    from app.main import app  # noqa: E402

    # ---- the recording fake Marzban -------------------------------------
    class FakeMarzban:
        """Benign answers + a public log. Read-only endpoints of the panel
        must not need Marzban at all; if one does, the sweep output shows
        exactly which call it tried."""

        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def _rec(self, method: str, *a):
            self.calls.append((method, str(a[:1])))
            return {}

        async def verify_admin_login(self, username: str, password: str) -> bool:
            self.calls.append(("verify_admin_login", username))
            return True

        async def get_admin_token(self, *a, **k):
            return "fake-token"

        async def list_all_users(self):
            self.calls.append(("list_all_users", ""))
            return []

        async def get_user(self, username: str):
            self.calls.append(("get_user", username))
            return {"username": username, "used_traffic": 0, "lifetime_used_traffic": 0,
                    "data_limit": 0, "expire": 0, "status": "active"}

        async def create_user(self, payload):
            return self._rec("create_user", payload) or dict(payload, used_traffic=0)

        async def modify_user(self, username, payload):
            return self._rec("modify_user", username) or {}

        async def delete_user(self, username):
            return self._rec("delete_user", username)

        async def reset_user(self, username):
            return self._rec("reset_user", username)

        def __getattr__(self, name):
            async def _any(*a, **k):
                return self._rec(name, a)
            return _any

    fake = FakeMarzban()
    # Bind the fake's BOUND methods onto the real singleton as instance
    # attributes — routers do `from app.marzban_client import marzban_client`,
    # which captured the object itself at import time, so replacing the
    # module attribute would silently leave every router on the real client.
    # (Same pattern as the test suite's `marzban_client.create_user = fake.create_user`.)
    real = marzban_module.marzban_client
    for name in ("verify_admin_login", "get_admin_token", "list_all_users",
                 "get_user", "create_user", "modify_user", "delete_user",
                 "reset_user"):
        setattr(real, name, getattr(fake, name))

    app.dependency_overrides[require_auth] = lambda: "harness-admin"

    # ---- static frontend with SPA fallback ------------------------------
    # Catch-all added LAST: every real /api/* route was registered at import
    # time and wins; anything not matching an API route resolves to a dist
    # file, else index.html (BrowserRouter deep links like /accounts).
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and str(candidate).startswith(str(dist)):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    print(f"harness: db={db}")
    print(f"harness: dist={dist}")
    print(f"harness: http://127.0.0.1:{args.port}  (Ctrl+C to stop; no scheduler, fake Marzban)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", lifespan="off")


if __name__ == "__main__":
    main()
