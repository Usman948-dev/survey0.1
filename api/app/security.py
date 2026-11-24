import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import config, permission
from .db import get_db
from .models import User


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


def make_token(username: str, kind: str) -> str:
    delta = (
        timedelta(minutes=config.ACCESS_MINUTES)
        if kind == "access"
        else timedelta(days=config.REFRESH_DAYS)
    )
    payload = {"sub": username, "typ": kind, "exp": datetime.now(timezone.utc) + delta}
    return jwt.encode(payload, config.SECRET_KEY, algorithm="HS256")


def decode_token(token: str, kind: str) -> str:
    try:
        p = jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    if p.get("typ") != kind:
        raise HTTPException(401, "Wrong token type")
    return p["sub"]


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    username = decode_token(auth[7:], "access")
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.active:
        raise HTTPException(401, "Account disabled")
    permission.require(db, user)
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admins only")
    return user


def project_user(project: str, user: User = Depends(current_user)) -> User:
    """Path parameter {project}: admins may read any project, a surveyor only their own."""
    if project not in config.PROJECTS:
        raise HTTPException(404, "No such project")
    if user.role != "admin" and user.project != project:
        raise HTTPException(403, "No access to this project")
    return user


# --- very small login throttle (per username+IP): 8 failures / 5 minutes ---
_fails: dict[str, list[float]] = {}


def throttle_check(key: str):
    now = time.time()
    recent = [t for t in _fails.get(key, []) if now - t < 300]
    _fails[key] = recent
    if len(recent) >= 8:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")


def throttle_fail(key: str):
    _fails.setdefault(key, []).append(time.time())


def throttle_clear(key: str):
    _fails.pop(key, None)
