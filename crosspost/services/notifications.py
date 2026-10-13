"""Notification service (email). Console backend for development, SMTP for production."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..logging_setup import get_logger, log_event
from ..models import Inquiry, Lead, Notification

log = get_logger(__name__)

OUTBOX: list[EmailMessage] = []  # console backend keeps messages here (useful in tests)


class EmailSendError(Exception):
    pass


def send_email(to: str, subject: str, body: str, reply_to: str | None = None) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.email_from
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if s.email_backend == "console":
        OUTBOX.append(msg)
        log_event(log, "email_console_backend", subject=subject)
        return
    if not s.smtp_host:
        raise EmailSendError("SMTP_HOST not configured")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            if s.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(f"SMTP send failed: {type(exc).__name__}") from exc


def lead_notification_body(lead: Lead, inquiry_message: str | None) -> str:
    s = get_settings()
    listing = lead.listing
    lines = [
        f"New buyer inquiry for MLS# {listing.mls_id} — {listing.display_address}",
        "",
        f"Name: {lead.contact.full_name}",
        f"Email: {lead.contact.email}",
        f"Phone: {lead.contact.phone or '—'}",
        f"Preferred contact: {lead.preferred_contact_method}",
        f"Source: {lead.source_platform}" + (f" (campaign {lead.campaign.code})" if lead.campaign else ""),
        "",
        "Message:",
        inquiry_message or "(none)",
        "",
        f"View in dashboard: {s.public_base_url}/admin/leads/{lead.id}",
        "",
        "This message contains personal information. Do not forward outside your organization.",
    ]
    return "\n".join(lines)


def notify_new_lead(db: Session, lead: Lead, inquiry: Inquiry) -> int:
    """Email the lead to the configured recipients. Safe to retry: recipients already
    notified about this inquiry are skipped, and each send is committed immediately."""
    recipients = list(get_settings().notification_recipients)
    agent_email = lead.listing.list_agent_email
    if not recipients and agent_email:
        recipients = [agent_email]
    kind = f"new_lead:{inquiry.id}"
    already = set(db.scalars(select(Notification.recipient).where(
        Notification.kind == kind, Notification.status == "sent")))
    sent = 0
    failures = []
    for to in recipients:
        if to in already:
            continue
        subject = f"New inquiry: {lead.listing.display_address} (MLS# {lead.listing.mls_id})"
        note = Notification(kind=kind, recipient=to, subject=subject, lead_id=lead.id)
        db.add(note)
        try:
            send_email(to, subject, lead_notification_body(lead, inquiry.message), reply_to=lead.contact.email)
            note.status, note.sent_at = "sent", utcnow()
            sent += 1
        except EmailSendError as exc:
            note.status, note.error = "failed", str(exc)
            failures.append(str(exc))
        db.commit()
    if not recipients:
        log_event(log, "lead_notification_no_recipients", lead_id=lead.id)
    if failures:
        raise EmailSendError("; ".join(failures))
    return sent


def notify_admins(db: Session, subject: str, body: str) -> None:
    for to in get_settings().notification_recipients:
        note = Notification(kind="system", recipient=to, subject=subject)
        db.add(note)
        try:
            send_email(to, subject, body)
            note.status, note.sent_at = "sent", utcnow()
        except EmailSendError as exc:
            note.status, note.error = "failed", str(exc)
