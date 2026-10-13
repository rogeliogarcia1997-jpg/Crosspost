"""Web dependencies: templates, authentication, role-based access control, CSRF."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import ROLE_ADMIN, ROLE_MARKETER, ROLE_VIEWER, AuditLog, User
from ..security import constant_time_equals, new_csrf_token

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _money(value: Any) -> str:
    return "—" if value is None else f"${value:,.0f}"


def _dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.strftime(fmt) + " UTC" if value else "—"


templates.env.filters["money"] = _money
templates.env.filters["dt"] = _dt
templates.env.globals["settings"] = get_settings


class LoginRequired(Exception):
    pass


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_csrf_token()
        request.session["csrf"] = token
    return token


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200):
    ctx = {"user": getattr(request.state, "user", None), "csrf_token": csrf_token(request),
           "flash": request.session.pop("flash", None), **(context or {})}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise LoginRequired()
    user = db.get(User, user_id)
    if user is None or not user.is_active or request.session.get("pwd") != user.password_hash[-16:]:
        request.session.clear()
        raise LoginRequired()
    request.state.user = user
    return user


ROLE_RANK = {ROLE_VIEWER: 1, ROLE_MARKETER: 2, ROLE_ADMIN: 3}


def require(min_role: str) -> Callable[..., User]:
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK.get(user.role, 0) < ROLE_RANK[min_role]:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
        return user

    return dep


require_viewer = require(ROLE_VIEWER)
require_marketer = require(ROLE_MARKETER)
require_admin = require(ROLE_ADMIN)


async def verify_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    form = await request.form()
    supplied = form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not constant_time_equals(str(supplied or ""), request.session.get("csrf")):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and try again.")


def audit(db: Session, user: User | None, action: str, entity_type: str | None = None, entity_id: Any = None,
          **details: Any) -> None:
    db.add(AuditLog(user_id=user.id if user else None, action=action, entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id is not None else None, details=details))


def client_ip(request: Request) -> str | None:
    # Behind a trusted reverse proxy, run uvicorn with --proxy-headers --forwarded-allow-ips.
    return request.client.host if request.client else None
