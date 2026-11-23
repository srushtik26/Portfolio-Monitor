from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import os
import secrets
import string
from typing import Annotated

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import ActivityLog, LoginRateLimit, User, UserSession

PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1, type=Type.ID)
SESSION_COOKIE = "portfolio_session"
CSRF_COOKIE = "portfolio_csrf"
SESSION_DURATION = timedelta(hours=max(1, int(os.environ.get("AUTH_SESSION_HOURS", "12"))))
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_ATTEMPTS = 5
DbSession = Annotated[Session, Depends(get_db)]


def normalize_name(name: str) -> tuple[str, str]:
    display_name = " ".join(name.strip().split())
    if not display_name or len(display_name) > 100:
        raise HTTPException(status_code=422, detail="Name must be between 1 and 100 characters.")
    return display_name, display_name.casefold()


def validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 256:
        raise HTTPException(status_code=422, detail="Password must be between 12 and 256 characters.")
    classes = (
        any(char.islower() for char in password),
        any(char.isupper() for char in password),
        any(char.isdigit() for char in password),
        any(not char.isalnum() for char in password),
    )
    if sum(classes) < 3:
        raise HTTPException(
            status_code=422,
            detail="Password must include at least three of lowercase, uppercase, number, and symbol.",
        )


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secure_cookie() -> bool:
    configured = os.environ.get("AUTH_COOKIE_SECURE")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get("APP_ENV", "development").strip().lower() == "production"


def issue_session(session: Session, user: User, response: Response) -> str:
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = _now()
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=_digest(raw_token),
            csrf_token_hash=_digest(csrf_token),
            expires_at=now + SESSION_DURATION,
            created_at=now,
        )
    )
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=int(SESSION_DURATION.total_seconds()),
        httponly=True,
        secure=_secure_cookie(),
        samesite="lax",
        path="/api",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=int(SESSION_DURATION.total_seconds()),
        httponly=False,
        secure=_secure_cookie(),
        samesite="lax",
        path="/api",
    )
    return csrf_token


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/api", secure=_secure_cookie(), httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/api", secure=_secure_cookie(), httponly=False, samesite="lax")


def record_activity(
    session: Session,
    user: User | None,
    action: str,
    details: dict | None = None,
) -> None:
    session.add(
        ActivityLog(
            user_id=user.id if user else None,
            action=action,
            details=details,
            timestamp=_now(),
        )
    )


def _allow_login_attempt(session: Session, request: Request, normalized_name: str) -> str:
    remote = request.client.host if request.client else "unknown"
    fingerprint = _digest(f"{normalized_name}\0{remote}")
    now = _now()
    row = session.get(LoginRateLimit, fingerprint)
    if row is None:
        row = LoginRateLimit(
            fingerprint=fingerprint,
            window_started_at=now,
            attempts=0,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    elif now - (
        row.window_started_at.replace(tzinfo=timezone.utc)
        if row.window_started_at.tzinfo is None
        else row.window_started_at.astimezone(timezone.utc)
    ) >= LOGIN_WINDOW:
        row.window_started_at = now
        row.attempts = 0
    if row.attempts >= LOGIN_MAX_ATTEMPTS:
        session.commit()
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")
    row.attempts += 1
    row.updated_at = now
    session.commit()
    return fingerprint


def get_current_user(request: Request, session: DbSession) -> User:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    session_row = session.scalar(
        select(UserSession).where(UserSession.token_hash == _digest(raw_token))
    )
    now = _now()
    expires_utc = (
        None
        if session_row is None
        else (
            session_row.expires_at.replace(tzinfo=timezone.utc)
            if session_row.expires_at.tzinfo is None
            else session_row.expires_at.astimezone(timezone.utc)
        )
    )
    if session_row is None or expires_utc is None or expires_utc <= now:
        if session_row is not None:
            session.delete(session_row)
            session.commit()
        raise HTTPException(status_code=401, detail="Session expired. Sign in again.")
    user = session.get(User, session_row.user_id)
    if user is None or not user.is_active:
        session.delete(session_row)
        session.commit()
        raise HTTPException(status_code=401, detail="Account is inactive or unavailable.")

    request.state.auth_user = user
    request.state.auth_session = session_row
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.headers.get("X-CSRF-Token", "")
        csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
        if (
            not csrf
            or not csrf_cookie
            or not hmac.compare_digest(csrf, csrf_cookie)
            or not hmac.compare_digest(_digest(csrf), session_row.csrf_token_hash)
        ):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")
    return user


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Administrator permission required.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]


def generate_temporary_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%_-+"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        try:
            validate_password(value)
            return value
        except HTTPException:
            continue