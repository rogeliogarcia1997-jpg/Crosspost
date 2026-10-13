"""OAuth 2.0 connection flow for social platforms (authorization code, with PKCE where supported)."""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import User
from ..services.accounts import connect_account
from ..social.base import PublishError
from ..social.registry import ADAPTERS, get_adapter
from .deps import audit, flash, require_admin, verify_csrf

router = APIRouter(prefix="/admin/oauth")


def _redirect_uri(platform: str) -> str:
    return f"{get_settings().public_base_url}/admin/oauth/{platform}/callback"


@router.post("/{platform}/start", dependencies=[Depends(verify_csrf)])
def start(platform: str, request: Request, external_account_id: str = Form(...), display_name: str = Form(...),
          user: User = Depends(require_admin)):
    if platform not in ADAPTERS:
        raise HTTPException(404, "Unknown platform.")
    cfg = get_adapter(platform).oauth_config()
    if cfg is None:
        flash(request, f"{platform} app credentials are not configured in the environment.", "error")
        return RedirectResponse("/admin/accounts", status_code=303)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48) if cfg.use_pkce else None
    request.session["oauth"] = {"state": state, "platform": platform, "verifier": verifier,
                                "external_account_id": external_account_id.strip()[:200],
                                "display_name": display_name.strip()[:200]}
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": _redirect_uri(platform),
        "scope": cfg.scope_separator.join(cfg.scopes),
        "state": state,
    }
    if verifier:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params.update(code_challenge=challenge, code_challenge_method="S256")
    return RedirectResponse(f"{cfg.authorize_url}?{urlencode(params)}", status_code=303)


@router.get("/{platform}/callback")
def callback(platform: str, request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    pending = request.session.pop("oauth", None)
    if not pending or pending.get("platform") != platform or not state or not secrets.compare_digest(state, pending["state"]):
        raise HTTPException(400, "Invalid OAuth state. Start the connection again.")
    if error or not code:
        flash(request, f"Authorization was not completed ({error or 'no code returned'}).", "error")
        return RedirectResponse("/admin/accounts", status_code=303)
    adapter = get_adapter(platform)
    try:
        tokens = adapter.exchange_code(code, _redirect_uri(platform), pending.get("verifier"))
        tokens, extra = adapter.finalize_connection(tokens, pending["external_account_id"])
    except PublishError as exc:
        flash(request, f"Could not complete connection: {exc}", "error")
        return RedirectResponse("/admin/accounts", status_code=303)
    account = connect_account(db, platform, pending["external_account_id"], pending["display_name"], tokens, extra or None)
    audit(db, user, "account_connected", "social_account", account.id, platform=platform, method="oauth")
    db.commit()
    flash(request, f"Connected {adapter.spec.display_name}.")
    return RedirectResponse("/admin/accounts", status_code=303)
