"""CRM integration layer.

``CRMClient.push_inquiry`` sends a lead/inquiry to an external CRM. Add a CRM
by subclassing :class:`CRMClient` and registering it in ``get_crm_client``.

* ``webhook``: signed JSON POST to any endpoint (Zapier, Make, a custom CRM).
* ``followupboss``: Follow Up Boss Events API (``POST /v1/events``), the
  vendor-recommended way to send new inquiries. Requires an API key and a
  registered system name/key. Validate with a test account before go-live.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..config import get_settings
from ..models import Inquiry, Lead


class CRMError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def inquiry_payload(lead: Lead, inquiry: Inquiry) -> dict[str, Any]:
    listing = lead.listing
    s = get_settings()
    return {
        "lead_id": lead.id,
        "inquiry_id": inquiry.id,
        "submitted_at": inquiry.submitted_at.isoformat() + "Z",
        "contact": {"name": lead.contact.full_name, "email": lead.contact.email, "phone": lead.contact.phone},
        "preferred_contact_method": inquiry.preferred_contact_method,
        "marketing_consent": lead.marketing_consent,
        "sms_consent": lead.sms_consent,
        "message": inquiry.message,
        "listing": {
            "mls_id": listing.mls_id,
            "address": listing.display_address,
            "city": listing.city,
            "state": listing.state,
            "postal_code": listing.postal_code,
            "price": str(listing.price) if listing.price is not None else None,
            "url": f"{s.public_base_url}/p/{listing.slug}",
        },
        "attribution": {
            "platform": inquiry.platform,
            "campaign": lead.campaign.code if lead.campaign else None,
            "utm_source": inquiry.utm_source,
            "utm_medium": inquiry.utm_medium,
            "utm_campaign": inquiry.utm_campaign,
            "utm_content": inquiry.utm_content,
        },
    }


def _post(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    try:
        resp = client.post(url, **kwargs)
    except httpx.HTTPError as exc:
        raise CRMError(f"CRM unreachable: {type(exc).__name__}", retryable=True) from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise CRMError(f"CRM temporary error ({resp.status_code})", retryable=True)
    if resp.status_code >= 400:
        raise CRMError(f"CRM rejected request ({resp.status_code})", retryable=False)
    return resp


class CRMClient(ABC):
    name: str

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=15)

    @abstractmethod
    def push_inquiry(self, lead: Lead, inquiry: Inquiry) -> str | None:
        ...


class WebhookCRM(CRMClient):
    name = "webhook"

    def push_inquiry(self, lead: Lead, inquiry: Inquiry) -> str | None:
        s = get_settings()
        if not s.crm_webhook_url:
            raise CRMError("CRM_WEBHOOK_URL not configured", retryable=False)
        raw = json.dumps(inquiry_payload(lead, inquiry), sort_keys=True).encode()
        headers = {"Content-Type": "application/json", "Idempotency-Key": f"crosspost-inquiry-{inquiry.id}"}
        if s.crm_webhook_secret:
            headers["X-Crosspost-Signature"] = "sha256=" + hmac.new(
                s.crm_webhook_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
        resp = _post(self.client, s.crm_webhook_url, content=raw, headers=headers)
        try:
            return str(resp.json().get("id")) if resp.content else None
        except ValueError:
            return None


class FollowUpBossCRM(CRMClient):
    name = "followupboss"
    url = "https://api.followupboss.com/v1/events"

    def push_inquiry(self, lead: Lead, inquiry: Inquiry) -> str | None:
        s = get_settings()
        if not s.followupboss_api_key:
            raise CRMError("FOLLOWUPBOSS_API_KEY not configured", retryable=False)
        payload = inquiry_payload(lead, inquiry)
        first, _, last = lead.contact.full_name.partition(" ")
        body = {
            "source": f"{s.app_name} ({inquiry.platform})",
            "system": s.followupboss_system,
            "type": "Property Inquiry",
            "message": inquiry.message or "",
            "person": {
                "firstName": first,
                "lastName": last,
                "emails": [{"value": lead.contact.email}],
                "phones": [{"value": lead.contact.phone}] if lead.contact.phone else [],
            },
            "property": {
                "street": lead.listing.street_address if lead.listing.address_display_allowed else None,
                "city": lead.listing.city,
                "state": lead.listing.state,
                "code": lead.listing.postal_code,
                "mlsNumber": lead.listing.mls_id,
                "price": float(lead.listing.price) if lead.listing.price is not None else None,
                "url": payload["listing"]["url"],
            },
            "campaign": {"source": inquiry.utm_source, "medium": inquiry.utm_medium, "campaign": inquiry.utm_campaign,
                         "content": inquiry.utm_content},
        }
        headers = {"X-System": s.followupboss_system}
        if s.followupboss_system_key:
            headers["X-System-Key"] = s.followupboss_system_key.get_secret_value()
        resp = _post(self.client, self.url, json=body, headers=headers,
                     auth=(s.followupboss_api_key.get_secret_value(), ""))
        try:
            data = resp.json()
            return str(data.get("id") or data.get("personId") or "") or None
        except ValueError:
            return None


_override: CRMClient | None = None


def set_crm_override(client: CRMClient | None) -> None:
    global _override
    _override = client


def get_crm_client() -> CRMClient | None:
    if _override is not None:
        return _override
    provider = get_settings().crm_provider
    if provider == "webhook":
        return WebhookCRM()
    if provider == "followupboss":
        return FollowUpBossCRM()
    return None
