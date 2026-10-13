"""Compliance checks: marketing authorization and fair-housing language screening.

The fair-housing screen is a safeguard, not legal advice. It flags phrases
commonly identified (e.g. in HUD advertising guidance) as indicating a
preference, limitation or discrimination based on a protected class. Flagged
content is excluded from generated posts and the post is routed to manual
review. Generated copy itself never references people, neighbourhood
demographics, or protected characteristics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Settings, get_settings
from ..models import Listing

_PROTECTED_GROUPS = (
    r"(?:white|black|hispanic|latino|latina|asian|caucasian|african[- ]american|christian|jewish|muslim|catholic|"
    r"hindu|ethnic|immigrant|foreign(?:er)?s?)"
)
FAIR_HOUSING_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("familial status", re.compile(r"\b(no|without)\s+(children|kids|minors)\b", re.I)),
    ("familial status", re.compile(r"\badults?\s+only\b", re.I)),
    ("familial status", re.compile(r"\b(perfect|ideal|great)\s+for\s+(a\s+|an\s+)?(young\s+|single\s+|married\s+|growing\s+)?(couple|couples|family|families|singles?|bachelors?|retirees|empty[- ]nesters|newlyweds|professionals?|students)\b", re.I)),
    ("familial status", re.compile(r"\b(empty[- ]nesters?|newlyweds|bachelor\s+pad)\b", re.I)),
    ("age", re.compile(r"\b(seniors?|mature\s+(persons?|individuals?|couples?|adults?))\s+only\b", re.I)),
    ("protected class", re.compile(rf"\b{_PROTECTED_GROUPS}\s+(neighbou?rhood|community|area|family|families|buyers?|residents?)\b", re.I)),
    ("protected class", re.compile(r"\b(integrated|segregated|restricted|exclusive)\s+(neighbou?rhood|community|area)\b", re.I)),
    ("religion", re.compile(r"\b(walking\s+distance|close|near|steps)\s+(to|from)\s+(the\s+)?(church|synagogue|mosque|temple|parish)\b", re.I)),
    ("disability", re.compile(r"\b(no\s+wheelchairs?|able[- ]bodied|must\s+(be\s+able\s+to\s+)?climb|not\s+suitable\s+for\s+(the\s+)?(handicapped|disabled))\b", re.I)),
    ("national origin", re.compile(r"\b(english[- ]speaking|english\s+only|must\s+speak\s+english)\b", re.I)),
    ("source of income", re.compile(r"\bno\s+(section\s*8|vouchers?|housing\s+assistance)\b", re.I)),
    ("steering", re.compile(r"\b(safe|crime[- ]free|good)\s+(neighbou?rhood|area|community)\b", re.I)),
]


@dataclass
class ScreenResult:
    flagged: bool
    findings: list[str]


def fair_housing_screen(text: str | None) -> ScreenResult:
    if not text:
        return ScreenResult(False, [])
    findings = []
    for category, pattern in FAIR_HOUSING_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(f"{category}: \"{match.group(0)}\"")
    return ScreenResult(bool(findings), findings)


@dataclass
class AuthorizationResult:
    authorized: bool
    reason: str


def marketing_authorization(listing: Listing, settings: Settings | None = None) -> AuthorizationResult:
    """Is this listing authorized for social-media marketing by this brokerage?

    MLS/IDX rules generally restrict syndicating other brokers' listings to
    social media; by default only the brokerage's own listings are marketed.
    """
    s = settings or get_settings()
    if not listing.internet_display_allowed:
        return AuthorizationResult(False, "Seller has opted out of internet display (InternetEntireListingDisplayYN = false)")
    if listing.marketing_override is False:
        return AuthorizationResult(False, "Marketing disabled for this listing by an administrator")
    if listing.marketing_override is True:
        return AuthorizationResult(True, "Marketing explicitly authorized by an administrator")
    if s.marketing_scope == "own_office":
        if listing.list_office_id and listing.list_office_id in s.office_ids:
            return AuthorizationResult(True, "Listing office is this brokerage")
        return AuthorizationResult(False, "Listing office is not in BROKERAGE_OFFICE_IDS")
    if s.marketing_scope == "own_agent":
        if listing.list_agent_id and listing.list_agent_id in s.agent_ids:
            return AuthorizationResult(True, "Listing agent is in BROKERAGE_AGENT_IDS")
        return AuthorizationResult(False, "Listing agent is not in BROKERAGE_AGENT_IDS")
    return AuthorizationResult(False, "Awaiting explicit authorization (MARKETING_SCOPE=flagged)")
