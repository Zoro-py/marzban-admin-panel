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
    jwt_expire_minutes: int = 1440

    bot_token: str = ""
    bot_admin_chat_id: str = ""
    bot_api_base_url: str = "http://127.0.0.1:8000"

    sync_interval_minutes: int = 60

    # Applied to a new Marzban user when the caller doesn't specify proxies/inbounds.
    # Adjust these to match this panel's real inbound tags before creating users from
    # the dashboard — Marzban applies a protocol to every inbound that supports it when
    # no explicit inbound tag list is given, which may not match what you actually want.
    marzban_default_proxies: dict[str, dict] = {"vless": {}, "vmess": {}, "trojan": {}, "shadowsocks": {}}
    marzban_default_inbounds: dict[str, list[str]] = {}


def _load_or_create_jwt_secret() -> str:
    secret_file = Path(__file__).resolve().parent.parent / ".jwt_secret"
    if secret_file.exists():
        return secret_file.read_text().strip()
    secret = secrets.token_hex(32)
    secret_file.write_text(secret)
    return secret


settings = Settings()
if not settings.jwt_secret:
    settings.jwt_secret = _load_or_create_jwt_secret()
