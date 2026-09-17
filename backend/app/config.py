import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    marzban_base_url: str
    # Service-account credentials Marzban itself already has — used for the backend's
    # own unattended calls (the nightly sync job runs with nobody logged in, so it needs
    # a stored credential regardless). This is deliberately the ONLY Marzban credential
    # anyone has to put in a config file: dashboard/bot login now authenticates directly
    # against Marzban's own /api/admin/token instead of a separately invented password
    # (see app.routers.auth), so there is nothing else to keep in sync.
    marzban_username: str
    marzban_password: str

    database_url: str = "sqlite:///./vpn.db"

    # Left blank on purpose — auto-generated and persisted to backend/.jwt_secret on
    # first run (see _load_or_create_jwt_secret below) so this is genuinely a "never
    # touch it" value, not one more env var to set.
    jwt_secret: str = ""
    # Where that auto-generated secret is kept. Blank = backend/.jwt_secret,
    # which is right for a local checkout and WRONG inside a container: that
    # path lives in the image layer, not in the data volume, so every
    # `docker compose up --build` produced a fresh secret and logged every
    # dashboard and bot session out — while backup_job.py was bundling the old
    # secret into the nightly archive specifically so that would not happen.
    # docker-compose.yml points this at /app/data/.jwt_secret, inside the
    # volume. Existing non-container installs leave it blank and are unaffected.
    jwt_secret_file: str = ""
    jwt_expire_minutes: int = 1440  # 1 day
    # "Remember me" checkbox on login uses this instead of jwt_expire_minutes.
    jwt_remember_expire_minutes: int = 43200  # 30 days

    bot_token: str = ""
    bot_admin_chat_id: str = ""
    bot_api_base_url: str = "http://127.0.0.1:8000"

    # How often the sync job runs — also how long a customer whose plan just
    # ran out stays disconnected before the next-plan feature can react, so
    # this is deliberately tight rather than the traditional "poll hourly"
    # default. 60s: Marzban runs on the same server (near-zero latency per
    # call), so the cost of polling more often is negligible; going lower
    # buys very little further (the human-perceived difference between a
    # 30s and 60s reconnect is nil) while raising the odds of a sync cycle
    # still running when the next one is due to start (see main.py's
    # max_instances=1 on this job — that cycle would just get skipped).
    sync_interval_seconds: int = 60

    # Nightly off-server backup (see backup_job.py): hour/minute (server's own
    # local time, 24h) it runs at. No enabled/disabled flag on purpose — it's
    # simply skipped, with a clear log line, whenever bot_token/bot_admin_chat_id
    # aren't set, rather than needing a second switch kept in sync with them.
    backup_hour: int = 3
    backup_minute: int = 30

    # payg_monthly_job checks daily at this hour/minute whether there's an
    # unsettled Jalali month to close out (see its own _target_settlement_period
    # for why "daily" and not "only on the last day" — a failed attempt keeps
    # retrying at this same time on every later day instead of silently
    # skipping the rest of the month). "Night" per the operator's own request.
    payg_monthly_settle_hour: int = 23
    payg_monthly_settle_minute: int = 30

    # debt_nudge_job's weekly overdue-debt summary (see its own docstring for
    # why once-a-week is the whole noise-control mechanism). "mon" = Monday,
    # a normal start-of-week check-in time; APScheduler's own day_of_week
    # names (mon/tue/.../sun).
    # Unused since the nudge moved to every-other-day scheduling (the job
    # skips odd calendar dates itself) — kept so existing .env files that
    # still set it keep parsing.
    debt_nudge_day_of_week: str = "mon"
    debt_nudge_hour: int = 9
    debt_nudge_minute: int = 0

    # Applied to a new Marzban user when the caller doesn't specify proxies/inbounds.
    # Adjust these to match this panel's real inbound tags before creating users from
    # the dashboard — Marzban applies a protocol to every inbound that supports it when
    # no explicit inbound tag list is given, which may not match what you actually want.
    marzban_default_proxies: dict[str, dict] = {"vless": {}, "vmess": {}, "trojan": {}, "shadowsocks": {}}
    marzban_default_inbounds: dict[str, list[str]] = {}

    # Marzban returns each user's `subscription_url` as a RELATIVE path
    # ("/sub/<token>") unless its own XRAY_SUBSCRIPTION_URL_PREFIX is set, in
    # which case it is already absolute. A relative path is useless to a
    # customer, so this backend resolves it against a base before handing it
    # out. Left blank, that base is MARZBAN_BASE_URL — correct for the common
    # setup where the panel and the subscription endpoint share a hostname.
    # Set this only when subscriptions are served from a DIFFERENT public
    # host than the panel itself (a separate sub-domain, a CDN in front of
    # it): getting it wrong produces links that resolve but 404, which looks
    # identical to a broken account from the customer's side.
    # Absolute subscription_url values from Marzban are passed through
    # untouched either way — this never rewrites a host Marzban chose.
    marzban_subscription_base_url: str = ""

    # Shared secret the customer-facing SHOP bot presents on /api/shop/bot/*.
    #
    # Deliberately NOT the Marzban admin credentials the operator's own bot
    # uses. That bot is single-operator and gated to one chat id; the shop bot
    # takes messages from the public, so it is the piece most likely to be
    # compromised — and if it held admin credentials, compromising it would
    # hand over settlements, backups and the whole ledger. With this, the
    # worst an attacker gains is the shop endpoints.
    #
    # Blank = every /api/shop/bot/* request is refused. Fail closed: a shop
    # bot that cannot authenticate must not fall back to working.
    shop_bot_api_key: str = ""

    # Telegram token + chat id for the customer-facing shop bot. The backend
    # needs the token to deliver a purchased account's QR directly to the
    # buyer, without routing it back through the bot process.
    shop_bot_token: str = ""

    # Shared secret delegate_bot/ presents on /api/delegate/bot/*. Same
    # fail-closed reasoning as shop_bot_api_key above: a Delegate is a
    # trusted customer, but the PROCESS talking to this key still runs on
    # infrastructure the operator doesn't control, so it gets a key that
    # reaches nothing but this router — not the ledger, not Marzban admin
    # credentials, not any other customer's accounts (that last part is
    # enforced per-request by delegate_service's exact customer/group scope
    # check, not by this key).
    delegate_bot_api_key: str = ""


DEFAULT_JWT_SECRET_PATH = Path(__file__).resolve().parent.parent / ".jwt_secret"


def jwt_secret_path(configured: str = "") -> Path:
    """The single source of truth for where the signing secret lives.

    backup_job.py imports this rather than recomputing the path. It used to
    keep its own copy of the same expression, which meant making the location
    configurable here would have silently left the backup archiving a file
    that no longer existed — a backup that "ran without error" and was quietly
    useless, which is a failure mode this project has already had once.
    """
    return Path(configured).expanduser() if configured else DEFAULT_JWT_SECRET_PATH


def _load_or_create_jwt_secret(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    # The configured location may be a volume mount that exists but has no
    # parent dirs yet on a first run.
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    # 0600: whoever can read this file can forge a login for any admin.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secret)
    return secret


settings = Settings()
JWT_SECRET_PATH = jwt_secret_path(settings.jwt_secret_file)
if not settings.jwt_secret:
    settings.jwt_secret = _load_or_create_jwt_secret(JWT_SECRET_PATH)
