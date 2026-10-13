"""Publishing rules: environment defaults, overridable from the dashboard."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppSetting

RULES_KEY = "publishing_rules"
PUBLISH_MODES = ("immediate", "scheduled", "manual_approval")


@dataclass
class PublishingRules:
    new_listing_mode: str = "manual_approval"
    delay_minutes: int = 0  # used by "scheduled" mode
    announce_price_reductions: bool = True
    price_reduction_mode: str = "manual_approval"
    announce_open_houses: bool = True
    open_house_mode: str = "immediate"
    open_house_lead_hours: int = 48
    # Status announcements (e.g. "Under contract", "Sold") never represent
    # availability. They always require manual approval because sellers or
    # brokers may need to authorise them.
    announce_status_changes: bool = False
    repost_interval_days: int = 0
    delete_posts_on_unavailable: bool = False
    enabled_platforms: list[str] = field(default_factory=lambda: ["test"])

    def mode_for(self, kind: str) -> str:
        if kind == "status_update":
            return "manual_approval"
        if kind == "price_reduction":
            return self.price_reduction_mode
        if kind == "open_house":
            return self.open_house_mode
        return self.new_listing_mode

    def validate(self) -> list[str]:
        errors = []
        for attr in ("new_listing_mode", "price_reduction_mode", "open_house_mode"):
            if getattr(self, attr) not in PUBLISH_MODES:
                errors.append(f"{attr} must be one of {', '.join(PUBLISH_MODES)}")
        if not 0 <= self.delay_minutes <= 60 * 24 * 30:
            errors.append("delay_minutes out of range")
        if not 0 <= self.open_house_lead_hours <= 24 * 14:
            errors.append("open_house_lead_hours out of range")
        if not 0 <= self.repost_interval_days <= 365:
            errors.append("repost_interval_days out of range")
        return errors


def default_rules() -> PublishingRules:
    s = get_settings()
    return PublishingRules(
        new_listing_mode=s.publish_mode_new_listing,
        delay_minutes=s.publish_delay_minutes,
        announce_price_reductions=s.announce_price_reductions,
        price_reduction_mode=s.publish_mode_new_listing,
        announce_open_houses=s.announce_open_houses,
        open_house_lead_hours=s.open_house_lead_hours,
        announce_status_changes=s.announce_status_changes,
        repost_interval_days=s.repost_interval_days,
        delete_posts_on_unavailable=s.delete_posts_on_unavailable,
        enabled_platforms=s.enabled_platform_list,
    )


def get_rules(db: Session) -> PublishingRules:
    rules = default_rules()
    row = db.get(AppSetting, RULES_KEY)
    if row and isinstance(row.value, dict):
        known = {f.name for f in fields(PublishingRules)}
        for key, value in row.value.items():
            if key in known:
                setattr(rules, key, value)
    return rules


def save_rules(db: Session, rules: PublishingRules) -> None:
    errors = rules.validate()
    if errors:
        raise ValueError("; ".join(errors))
    data: dict[str, Any] = asdict(rules)
    row = db.get(AppSetting, RULES_KEY)
    if row:
        row.value = data
    else:
        db.add(AppSetting(key=RULES_KEY, value=data))
