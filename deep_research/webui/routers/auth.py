"""Account auth API — register, verify, login, logout, session info, reset.

All endpoints live under ``/api/auth/`` and are CSRF-exempt (they run before a
session exists, or merely revoke one). Every other state-changing endpoint
requires the SPA's ``X-Requested-With`` header — enforced by the middleware in
``webui/app.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from deep_research.accounts.rows import UserRow
from deep_research.accounts.service import (
    SESSION_COOKIE,
    AuthError,
    AuthService,
    normalize_email,
)
from deep_research.webui.deps import get_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _auth(request: Request) -> AuthService:
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        raise HTTPException(
            status_code=400,
            detail="multi_user mode is not enabled on this server (pdl.auth.mode)",
        )
    return auth


def _map_auth_error(exc: AuthError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)


class VerifyRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    token: str = Field(min_length=10, max_length=256)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    token: str = Field(min_length=10, max_length=256)
    new_password: str = Field(min_length=1, max_length=128)


class SessionUserResponse(BaseModel):
    user_id: str
    email: str
    display_name: str | None = None
    auth_mode: str


class AuthStatusResponse(BaseModel):
    """Always-200 auth probe — lets the SPA boot without a 401 in the console."""

    authenticated: bool
    auth_mode: str
    user: SessionUserResponse | None = None


class RegisterResponse(BaseModel):
    status: str
    email: str


def _user_response(user: UserRow | None, *, auth_mode: str) -> SessionUserResponse:
    if user is None:  # pragma: no cover — callers re-read the row after minting
        raise HTTPException(status_code=500, detail="account disappeared")
    return SessionUserResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        auth_mode=auth_mode,
    )


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max(_auth(request).session_ttl_days, 1) * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/register", response_model=RegisterResponse, status_code=202)
async def register(body: RegisterRequest, request: Request) -> RegisterResponse:
    try:
        result = await _auth(request).register(
            body.email, body.password, display_name=body.display_name
        )
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    return RegisterResponse(status=result.status, email=result.email)


@router.post("/verify", response_model=SessionUserResponse)
async def verify(body: VerifyRequest, request: Request, response: Response):
    auth = _auth(request)
    try:
        token = await auth.verify(body.email, body.token)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    _set_session_cookie(request, response, token)
    user = await auth.get_user_by_email(normalize_email(body.email))
    return _user_response(user, auth_mode="multi_user")


@router.post("/resend-verification", status_code=202)
async def resend_verification(body: EmailRequest, request: Request) -> dict:
    try:
        await _auth(request).resend_verification(body.email)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    return {"status": "ok"}


@router.post("/login", response_model=SessionUserResponse)
async def login(body: LoginRequest, request: Request, response: Response):
    auth = _auth(request)
    try:
        token = await auth.login(body.email, body.password)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    _set_session_cookie(request, response, token)
    user = await auth.get_user_by_email(normalize_email(body.email))
    return _user_response(user, auth_mode="multi_user")


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    auth = getattr(request.app.state, "auth", None)
    if auth is not None:
        await auth.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=SessionUserResponse)
async def me(request: Request) -> SessionUserResponse:
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    mode = getattr(request.app.state, "auth_mode", "single_user")
    return _user_response(user, auth_mode=mode)


@router.get("/status", response_model=AuthStatusResponse)
async def status(request: Request) -> AuthStatusResponse:
    """Auth probe that never 401s — used by the SPA's boot sequence."""
    user = get_user(request)
    mode = getattr(request.app.state, "auth_mode", "single_user")
    if user is None:
        return AuthStatusResponse(authenticated=False, auth_mode=mode)
    return AuthStatusResponse(
        authenticated=True, auth_mode=mode, user=_user_response(user, auth_mode=mode)
    )


@router.post("/request-password-reset", status_code=202)
async def request_password_reset(body: EmailRequest, request: Request) -> dict:
    try:
        await _auth(request).request_password_reset(body.email)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    return {"status": "ok"}


@router.post("/reset", response_model=SessionUserResponse)
async def reset(body: ResetRequest, request: Request, response: Response):
    auth = _auth(request)
    try:
        token = await auth.reset_password(body.email, body.token, body.new_password)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    _set_session_cookie(request, response, token)
    user = await auth.get_user_by_email(normalize_email(body.email))
    return _user_response(user, auth_mode="multi_user")
