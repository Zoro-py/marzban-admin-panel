from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel
import time
from collections import defaultdict

from app.auth import create_access_token
from app.marzban_client import MarzbanUnavailable, marzban_client

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

# Rate limiting simple dictionary cache
# Structure: { ip: {"attempts": int, "blocked_until": float, "last_seen": float} }
FAILED_LOGIN_ATTEMPTS = defaultdict(lambda: {"attempts": 0, "blocked_until": 0.0, "last_seen": 0.0})

def get_client_ip(request: Request) -> str:
    # X-Real-IP, not X-Forwarded-For: this deployment's own nginx config
    # (scripts/install.sh) sets X-Real-IP to $remote_addr directly, which
    # nginx always overwrites with the real TCP peer — unspoofable by the
    # client. X-Forwarded-For is set via $proxy_add_x_forwarded_for, which
    # APPENDS to whatever value the client already sent rather than
    # replacing it, so a client can prepend a fake IP and this rate limiter
    # would key off the fake one (the first entry) instead of the real one
    # nginx appended last.
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    # Fallback to standard request client host (e.g. running without nginx in front)
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request) -> LoginResponse:
    """Authenticates with the exact same credentials as the Marzban admin panel —
    there is no separate dashboard password to invent or keep in sync. Any admin
    account Marzban itself accepts is accepted here too."""
    ip = get_client_ip(request)
    now = time.time()
    
    # Garbage collection to prevent memory leak
    if len(FAILED_LOGIN_ATTEMPTS) > 1000:
        expired = [k for k, v in FAILED_LOGIN_ATTEMPTS.items() if v["last_seen"] < now - 3600]
        for k in expired:
            del FAILED_LOGIN_ATTEMPTS[k]
    
    # Check rate limit
    ip_data = FAILED_LOGIN_ATTEMPTS[ip]
    ip_data["last_seen"] = now
    if ip_data["blocked_until"] > now:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed login attempts. Please try again later."
        )
    
    # Reset attempts if block has expired
    if ip_data["blocked_until"] != 0.0 and ip_data["blocked_until"] <= now:
        ip_data["attempts"] = 0
        ip_data["blocked_until"] = 0.0

    try:
        ok = await marzban_client.verify_admin_login(body.username, body.password)
    except MarzbanUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))

    if not ok:
        ip_data["attempts"] += 1
        if ip_data["attempts"] >= 5:
            # Block for 5 minutes (300 seconds)
            ip_data["blocked_until"] = now + 300
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect Marzban username or password")
        
    # On successful login, reset failed attempts
    if ip in FAILED_LOGIN_ATTEMPTS:
        del FAILED_LOGIN_ATTEMPTS[ip]
        
    return LoginResponse(access_token=create_access_token(body.username, remember_me=body.remember_me))
