from datetime import datetime, timezone
from getpass import getpass
import os

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import SessionLocal, ensure_sqlite_schema
from .models import ActivityLog, User
from .security import hash_password, normalize_name


def bootstrap_admin_from_env() -> bool:
    """Create the initial ADMIN account from BOOTSTRAP_ADMIN_NAME and BOOTSTRAP_ADMIN_PASSWORD if no admin exists."""
    raw_name = os.environ.get("BOOTSTRAP_ADMIN_NAME", "").strip()
    raw_password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not raw_name or not raw_password:
        return False
    ensure_sqlite_schema()
    with SessionLocal() as session:
        if session.scalar(select(User.id).where(User.role == "ADMIN").limit(1)) is not None:
            return False
        name, normalized_name = normalize_name(raw_name)
        now = datetime.now(timezone.utc)
        user = User(
            name=name,
            normalized_name=normalized_name,
            password_hash=hash_password(raw_password),
            role="ADMIN",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        try:
            session.flush()
            session.add(
                ActivityLog(
                    user_id=user.id,
                    action="admin_bootstrapped",
                    details={"name": name, "source": "env"},
                    timestamp=now,
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            return False
    return True


def main() -> None:
    ensure_sqlite_schema()
    with SessionLocal() as session:
        if session.scalar(select(User.id).where(User.role == "ADMIN").limit(1)) is not None:
            raise SystemExit("An administrator is already configured.")
        env_name = os.environ.get("BOOTSTRAP_ADMIN_NAME", "").strip()
        env_password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
        if env_name and env_password:
            name, normalized_name = normalize_name(env_name)
            password = env_password
        else:
            name, normalized_name = normalize_name(input("Administrator name: "))
            password = getpass("Administrator password (hidden): ")
            confirmation = getpass("Confirm password: ")
            if password != confirmation:
                raise SystemExit("Passwords did not match.")
        now = datetime.now(timezone.utc)
        user = User(
            name=name,
            normalized_name=normalized_name,
            password_hash=hash_password(password),
            role="ADMIN",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.flush()
        session.add(
            ActivityLog(
                user_id=user.id,
                action="admin_bootstrapped",
                details={"name": name, "source": "cli"},
                timestamp=now,
            )
        )
        session.commit()
    print(f"Administrator account created for {name}.")


if __name__ == "__main__":
    main()
