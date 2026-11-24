"""Daily permission with a "open the app within N hours" rule.

State of a surveyor, worked out from two timestamps (no separate "locked" flag to get out of sync):
  none     - the admin never gave permission
  expired  - the last permission was for an earlier day (it ends at midnight, local TIMEZONE)
  waiting  - given today, the user has not opened the app yet and still has time
  locked   - given today, but the user did not open the app within PERMISSION_OPEN_HOURS
  ok       - given today and the user opened the app in time
  not_required - admins, or the feature is switched off
Allowed to use the server: ok, waiting, not_required. The admin gives permission again to unlock.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import config
from .models import User

ALLOWED = ("ok", "waiting", "not_required")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite hands back naive datetimes (stored as UTC)
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _local_date(dt: datetime):
    return dt.astimezone(ZoneInfo(config.TIMEZONE)).date()


def evaluate(user: User, at: datetime | None = None) -> dict:
    at = at or now()
    if user.role == "admin" or not config.PERMISSION_REQUIRED:
        return {"state": "not_required", "granted_at": None, "opened_at": None, "locks_at": None}
    granted, opened = _aware(user.permission_granted_at), _aware(user.permission_opened_at)
    out = {"state": "none", "granted_at": granted, "opened_at": opened, "locks_at": None}
    if granted is None:
        return out
    if _local_date(granted) != _local_date(at):
        out["state"] = "expired"
    elif opened is not None:
        out["state"] = "ok"
    else:
        out["locks_at"] = granted + timedelta(hours=config.PERMISSION_OPEN_HOURS)
        out["state"] = "locked" if at >= out["locks_at"] else "waiting"
    return out


def view(user: User) -> dict:
    """JSON-friendly version of evaluate() for the admin page and /me."""
    e = evaluate(user)
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in e.items()}


def message(state: str) -> str:
    if state == "locked":
        return (f"Your account is locked because you did not open the app within "
                f"{config.PERMISSION_OPEN_HOURS:g} hours. Ask the admin to give you permission again.")
    if state == "expired":
        return "Your permission was for an earlier day. Ask the admin to give you permission for today."
    return "You have no permission yet. Ask the admin to give you permission."


def require(db: Session, user: User) -> None:
    """Called for every signed-in request. Refuses (423) when the user has no valid permission;
    the first allowed request after the admin gave permission counts as "opened the app"."""
    e = evaluate(user)
    if e["state"] not in ALLOWED:
        raise HTTPException(423, message(e["state"]))
    if e["state"] == "waiting":
        user.permission_opened_at = now()
        db.commit()


def grant(db: Session, user: User) -> None:
    """Gives permission for today and starts the "open within N hours" time again."""
    user.permission_granted_at = now()
    user.permission_opened_at = None
    db.commit()
