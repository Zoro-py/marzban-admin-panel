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
# Structure: { ip: {"attempts": int, "blocked_until": float} }
FAILED_LOGIN_ATTEMPTS = defaultdict(lambda: {"attempts": 0, "blocked_until": 0.0})

def get_client_ip(request: Request) -> str:
    # Try x-forwarded-for first
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # X-Forwarded-For can contain a comma-separated list of IPs, the first one is the client
        return forwarded.split(",")[0].strip()
    
    # Fallback to standard request client host
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
    
    # Check rate limit
    ip_data = FAILED_LOGIN_ATTEMPTS[ip]
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
