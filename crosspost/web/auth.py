"""Login / logout."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db, utcnow
from ..logging_setup import get_logger, log_event
from ..models import User
from ..security import hash_ip, rate_limited, verify_password
from .deps import audit, client_ip, render, verify_csrf

router = APIRouter()
log = get_logger(__name__)


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/admin") and "//" not in target:
        return target
    return "/admin"


@router.get("/login")
def login_form(request: Request, next: str | None = None):
    return render(request, "login.html", {"next": _safe_next(next)})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/admin"),
          db: Session = Depends(get_db)):
    email = email.strip().lower()
    ip_key = hash_ip(client_ip(request)) or "unknown"
    if rate_limited(db, "login_ip", ip_key, 20, 900) or rate_limited(db, "login_email", email, 10, 900):
        db.commit()
        return render(request, "login.html", {"error": "Too many attempts. Try again in 15 minutes.", "next": _safe_next(next)}, 429)
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        db.commit()
        log_event(log, "login_failed")
        return render(request, "login.html", {"error": "Invalid email or password.", "next": _safe_next(next)}, 401)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["pwd"] = user.password_hash[-16:]  # invalidates sessions when the password changes
    user.last_login_at = utcnow()
    audit(db, user, "login")
    db.commit()
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
