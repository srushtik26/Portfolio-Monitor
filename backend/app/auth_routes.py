import secrets
from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .database import get_db
from .models import ActivityLog, LoginRateLimit, NotificationPreference, User, UserSession
from .notifications import get_or_create_preferences
from .security import (
    AdminUser,
    CurrentUser,
    SESSION_DURATION,
    CSRF_COOKIE,
    _allow_login_attempt,
    _digest,
    _now,
    clear_session_cookie,
    generate_temporary_password,
    hash_password,
    issue_session,
    normalize_name,
    record_activity,
    validate_password,
    verify_password,
    _secure_cookie,
    PASSWORD_HASHER,
)

router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=256)
    email: str | None = Field(default=None, max_length=320)


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    is_active: bool | None = None


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def _normalize_email(raw_email: str | None) -> str | None:
    if raw_email is None:
        return None
    cleaned = raw_email.strip().lower()
    if not cleaned:
        return None
    if "@" not in cleaned or "." not in cleaned.split("@")[-1]:
        raise HTTPException(status_code=422, detail=f"Invalid email address: {cleaned}")
    return cleaned


def _sync_email_to_preferences(session, email: str | None) -> None:
    if not email:
        return
    pref: NotificationPreference = get_or_create_preferences(session)
    existing = list(pref.email_recipients or [])
    lower_set = {addr.strip().lower() for addr in existing if addr and addr.strip()}
    if email.lower() not in lower_set:
        pref.email_recipients = [*existing, email]
    pref.email_enabled = True
    pref.updated_at = _now()


def _user_payload(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "last_login_at": user.last_login_at,
    }


@router.get("/auth/setup-status")
def setup_status(session=Depends(get_db)) -> dict[str, bool]:
    has_admin = session.scalar(select(User.id).where(User.role == "ADMIN").limit(1)) is not None
    return {"admin_configured": has_admin}


@router.post("/auth/setup-admin", status_code=201)
def setup_initial_admin(
    payload: UserCreateRequest,
    response: Response,
    session=Depends(get_db),
) -> dict[str, object]:
    if session.scalar(select(User.id).where(User.role == "ADMIN").limit(1)) is not None:
        raise HTTPException(status_code=409, detail="An administrator account is already configured.")
    name, normalized = normalize_name(payload.name)
    email = _normalize_email(payload.email)
    now = _now()
    user = User(
        name=name,
        normalized_name=normalized,
        email=email,
        password_hash=hash_password(payload.password),
        role="ADMIN",
        is_active=True,
        created_at=now,
        updated_at=now,
        last_login_at=now,
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="A user with that name already exists.") from None
    _sync_email_to_preferences(session, email)
    csrf_token = issue_session(session, user, response)
    record_activity(session, user, "admin_bootstrapped", {"name": name, "email": email, "source": "setup_ui"})
    session.commit()
    session.refresh(user)
    return {
        "user": _user_payload(user),
        "csrf_token": csrf_token,
        "expires_in": int(SESSION_DURATION.total_seconds()),
    }


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response, session=Depends(get_db)) -> dict[str, object]:
    display_name, normalized = normalize_name(payload.name)
    fingerprint = _allow_login_attempt(session, request, normalized)
    user = session.scalar(select(User).where(User.normalized_name == normalized))
    valid = user is not None and verify_password(user.password_hash, payload.password)
    if user is None:
        # Match the expensive password-hash work for unknown names without a reusable dummy credential.
        PASSWORD_HASHER.hash(secrets.token_urlsafe(24))
    if not valid or user is None or not user.is_active:
        record_activity(session, user if user and user.is_active else None, "login_failed", {"name": display_name})
        session.commit()
        raise HTTPException(status_code=401, detail="Name or password is incorrect.")

    session.execute(delete(LoginRateLimit).where(LoginRateLimit.fingerprint == fingerprint))
    now = _now()
    user.last_login_at = now
    user.updated_at = user.updated_at or now
    csrf_token = issue_session(session, user, response)
    record_activity(session, user, "login", {})
    session.commit()
    return {"user": _user_payload(user), "csrf_token": csrf_token, "expires_in": int(SESSION_DURATION.total_seconds())}


@router.post("/auth/logout")
def logout(request: Request, response: Response, user: CurrentUser, session=Depends(get_db)) -> dict[str, str]:
    session_row = getattr(request.state, "auth_session", None)
    if session_row is not None:
        record_activity(session, user, "logout", {})
        session.delete(session_row)
        session.commit()
    clear_session_cookie(response)
    return {"status": "logged_out"}


@router.get("/auth/me")
def current_user(
    request: Request,
    user: CurrentUser,
    response: Response,
    session=Depends(get_db),
) -> dict[str, object]:
    session_row = getattr(request.state, "auth_session", None)
    if session_row is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    csrf_token = request.cookies.get(CSRF_COOKIE, "")
    if not csrf_token or not compare_digest(_digest(csrf_token), session_row.csrf_token_hash):
        csrf_token = secrets.token_urlsafe(32)
        session_row.csrf_token_hash = _digest(csrf_token)
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=int(SESSION_DURATION.total_seconds()),
            httponly=False,
            secure=_secure_cookie(),
            samesite="lax",
            path="/api",
        )
        session.commit()
        return {"user": _user_payload(user), "csrf_token": csrf_token}
    return {"user": _user_payload(user), "csrf_token": csrf_token}


@router.post("/auth/change-password")
def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    user: CurrentUser,
    session=Depends(get_db),
) -> dict[str, object]:
    if not verify_password(user.password_hash, payload.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    validate_password(payload.new_password)
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=422, detail="Choose a password you have not used for this account.")
    user.password_hash = hash_password(payload.new_password)
    user.updated_at = _now()
    session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    csrf_token = issue_session(session, user, response)
    record_activity(session, user, "password_changed", {})
    session.commit()
    return {"user": _user_payload(user), "csrf_token": csrf_token}


@router.get("/users")
def list_users(_admin: AdminUser, session=Depends(get_db)) -> list[dict[str, object]]:
    users = session.scalars(select(User).order_by(User.role, User.normalized_name))
    return [_user_payload(user) for user in users]


@router.post("/users", status_code=201)
def create_employee(payload: UserCreateRequest, admin: AdminUser, session=Depends(get_db)) -> dict[str, object]:
    name, normalized = normalize_name(payload.name)
    email = _normalize_email(payload.email)
    user = User(
        name=name,
        normalized_name=normalized,
        email=email,
        password_hash=hash_password(payload.password),
        role="EMPLOYEE",
        is_active=True,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(user)
    _sync_email_to_preferences(session, email)
    record_activity(session, admin, "employee_created", {"name": name, "email": email})
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="A user with that name already exists.") from None
    session.refresh(user)
    return _user_payload(user)


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdateRequest, admin: AdminUser, session=Depends(get_db)) -> dict[str, object]:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.role == "ADMIN" and payload.is_active is False:
        admin_count = len(session.scalars(select(User.id).where(User.role == "ADMIN", User.is_active.is_(True))).all())
        if user.id == admin.id or admin_count <= 1:
            raise HTTPException(status_code=409, detail="The active administrator account cannot be disabled.")
    if payload.name is not None:
        user.name, user.normalized_name = normalize_name(payload.name)
    if "email" in payload.model_fields_set:
        user.email = _normalize_email(payload.email)
        _sync_email_to_preferences(session, user.email)
    if payload.is_active is not None:
        user.is_active = payload.is_active
    user.updated_at = _now()
    if not user.is_active:
        session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    record_activity(session, admin, "user_updated", {"user_id": user.id, "email": user.email, "is_active": user.is_active})
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="A user with that name already exists.") from None
    session.refresh(user)
    return _user_payload(user)


@router.post("/users/{user_id}/reset-password")
def reset_user_password(user_id: int, admin: AdminUser, session=Depends(get_db)) -> dict[str, object]:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.role == "ADMIN":
        raise HTTPException(status_code=403, detail="Administrator passwords cannot be reset through employee management.")
    temporary_password = generate_temporary_password()
    user.password_hash = hash_password(temporary_password)
    user.updated_at = _now()
    session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    record_activity(session, admin, "employee_password_reset", {"user_id": user.id})
    session.commit()
    return {"user": _user_payload(user), "temporary_password": temporary_password}


@router.get("/activity")
def list_activity(_admin: AdminUser, session=Depends(get_db), limit: int = 200) -> list[dict[str, object]]:
    limit = min(500, max(1, limit))
    rows = session.execute(
        select(ActivityLog, User.name)
        .outerjoin(User, User.id == ActivityLog.user_id)
        .order_by(ActivityLog.timestamp.desc(), ActivityLog.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "user_id": row.user_id,
            "name": name,
            "action": row.action,
            "details": row.details,
            "timestamp": row.timestamp,
        }
        for row, name in rows
    ]