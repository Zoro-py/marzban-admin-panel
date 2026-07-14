from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

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


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Authenticates with the exact same credentials as the Marzban admin panel —
    there is no separate dashboard password to invent or keep in sync. Any admin
    account Marzban itself accepts is accepted here too."""
    try:
        ok = await marzban_client.verify_admin_login(body.username, body.password)
    except MarzbanUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))

    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect Marzban username or password")
    return LoginResponse(access_token=create_access_token(body.username, remember_me=body.remember_me))
