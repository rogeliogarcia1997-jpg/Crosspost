"""Platform-specific marketing content generation.

Content is assembled deterministically from MLS fields only. Nothing is
invented: a sentence or bullet is included only when the underlying field is
present. The MLS public remarks are excerpted verbatim (truncated at a
sentence boundary) and are screened for fair-housing issues; flagged remarks
and features are omitted and the post is routed to manual review.

Each platform's :class:`PlatformSpec` drives formatting. When the full post
does not fit, optional sections are dropped in a fixed order (description,
then highlights, then hashtags). Required sections (headline, price/location,
call to action with link, brokerage attribution) are never dropped; if they
cannot fit, generation fails rather than publishing a non-compliant post.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..config import get_settings
from ..models import Listing
from ..social.base import MediaItem, PlatformSpec, RenderedPost
from .compliance import fair_housing_screen

PROMOTIONAL_KINDS = {"new_listing", "price_reduction", "open_house", "repost", "back_on_market"}
POST_KINDS = PROMOTIONAL_KINDS | {"status_update"}


class ContentError(Exception):
    pass


_PROPERTY_WORDS = {
    "single family residence": "single-family home",
    "single family": "single-family home",
    "residential": "home",
    "condominium": "condominium",
    "townhouse": "townhouse",
    "manufactured home": "manufactured home",
    "duplex": "duplex",
    "triplex": "triplex",
    "quadruplex": "fourplex",
    "residential income": "income property",
    "land": "lot",
    "farm": "farm property",
    "residential lease": "rental home",
    "commercial sale": "commercial property",
}


def property_word(listing: Listing) -> str:
    for value in (listing.property_subtype, listing.property_type):
        if value:
            return _PROPERTY_WORDS.get(value.strip().lower(), value.strip().lower())
    return "property"


def fmt_price(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"${value:,.0f}"


def fmt_baths(listing: Listing) -> str | None:
    if listing.bathrooms_total is not None:
        value = Decimal(listing.bathrooms_total)
        return f"{value.normalize():f}" if value % 1 else f"{int(value)}"
    if listing.bathrooms_full is not None:
        total = listing.bathrooms_full + 0.5 * (listing.bathrooms_half or 0)
        return f"{total:g}"
    return None


def fmt_lot(listing: Listing) -> str | None:
    if listing.lot_size_acres is not None and listing.lot_size_acres >= Decimal("0.25"):
        return f"{Decimal(listing.lot_size_acres).quantize(Decimal('0.01')).normalize():f}-acre lot"
    if listing.lot_size_sqft:
        return f"{listing.lot_size_sqft:,} sq ft lot"
    return None


def location(listing: Listing) -> str:
    return ", ".join(p for p in [listing.city, listing.state] if p) or "the area"


def _tagify(text: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.findall(r"[A-Za-z0-9]+", text))


def fmt_open_house(oh: dict[str, Any]) -> str:
    tz = ZoneInfo(get_settings().display_timezone)
    start = datetime.fromisoformat(oh["start"]).replace(tzinfo=timezone.utc).astimezone(tz)
    end = datetime.fromisoformat(oh["end"]).replace(tzinfo=timezone.utc).astimezone(tz)

    def t(d: datetime) -> str:
        return d.strftime("%I:%M %p").lstrip("0")

    return f"{start.strftime('%a, %b')} {start.day}, {t(start)}–{t(end)} {start.strftime('%Z')}".strip()


def excerpt(text: str | None, limit: int) -> str | None:
    if not text or limit < 40:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if boundary >= limit * 0.4:
        return cut[: boundary + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:") + "…"


@dataclass
class ListingFacts:
    """Screened, display-ready facts derived from a listing."""

    price: str | None
    location: str
    property_word: str
    beds: int | None
    baths: str | None
    sqft: int | None
    lot: str | None
    year_built: int | None
    features: list[str]
    description: str | None
    warnings: list[str]
    requires_review: bool


def listing_facts(listing: Listing) -> ListingFacts:
    warnings: list[str] = []
    review = False
    features = []
    for feature in listing.features or []:
        screen = fair_housing_screen(feature)
        if screen.flagged:
            warnings.append(f"Feature omitted (fair housing review): {'; '.join(screen.findings)}")
            review = True
        else:
            features.append(feature)
    description = listing.description
    screen = fair_housing_screen(description)
    if screen.flagged:
        warnings.append("MLS remarks omitted pending fair housing review: " + "; ".join(screen.findings))
        description, review = None, True
    if listing.price is None:
        warnings.append("Listing has no price in the MLS feed")
        review = True
    if not listing.authorized_photos:
        warnings.append("No photos authorized for redistribution")
    return ListingFacts(
        price=fmt_price(listing.price),
        location=location(listing),
        property_word=property_word(listing),
        beds=listing.bedrooms,
        baths=fmt_baths(listing),
        sqft=listing.living_area_sqft,
        lot=fmt_lot(listing),
        year_built=listing.year_built,
        features=features,
        description=description,
        warnings=warnings,
        requires_review=review,
    )


def attribution(listing: Listing, short: bool = False) -> str:
    s = get_settings()
    office = listing.list_office_name or s.brokerage_name
    if not office:
        raise ContentError("Listing brokerage attribution is unavailable; cannot generate a compliant post")
    if short:
        text = f"Listing courtesy of {office}."
    else:
        agent = f"{listing.list_agent_name}, " if listing.list_agent_name else ""
        text = f"Listing courtesy of {agent}{office}."
        if s.brokerage_name and s.brokerage_name != office:
            text += f" Presented by {s.brokerage_name}."
    if s.post_attribution_suffix:
        text += f" {s.post_attribution_suffix}"
    return text


def headline(listing: Listing, kind: str, extra: dict[str, Any]) -> str:
    loc = location(listing)
    if kind == "new_listing":
        return f"NEW LISTING | Discover your next home in {loc}!"
    if kind == "price_reduction":
        return f"PRICE IMPROVEMENT | Now {fmt_price(listing.price)} in {loc}"
    if kind == "open_house":
        return f"OPEN HOUSE | {fmt_open_house(extra['open_house'])} in {loc}"
    if kind == "repost":
        return f"FEATURED LISTING | Take another look at this home in {loc}"
    if kind == "back_on_market":
        return f"BACK ON THE MARKET | Available again in {loc}"
    if kind == "status_update":
        label = {"Closed": "SOLD", "Pending": "UNDER CONTRACT", "Active Under Contract": "UNDER CONTRACT"}.get(
            listing.status, listing.status.upper()
        )
        return f"{label} | {listing.display_address}"
    raise ContentError(f"unknown post kind {kind}")


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


STATUS_WORDS = {"Closed": "sold", "Pending": "under contract", "Active Under Contract": "under contract"}


def summary_sentence(listing: Listing, facts: ListingFacts, kind: str, extra: dict[str, Any]) -> str:
    if kind == "status_update":
        word = STATUS_WORDS.get(listing.status, listing.status.lower())
        return f"This {facts.property_word} in {facts.location} is now {word}."
    specs = []
    if facts.beds is not None:
        specs.append(f"{facts.beds} bedroom{'s' if facts.beds != 1 else ''}")
    if facts.baths is not None:
        specs.append(f"{facts.baths} bathroom{'s' if facts.baths != '1' else ''}")
    if facts.sqft:
        specs.append(f"{facts.sqft:,} square feet of living space")
    if facts.price:
        if kind == "price_reduction" and extra.get("old_price"):
            lead = f"Now priced at {facts.price} (previously {fmt_price(Decimal(str(extra['old_price'])))}), this"
        else:
            lead = f"Priced at {facts.price}, this"
    else:
        lead = "This"
    if specs:
        return f"{lead} {facts.property_word} features {_join(specs)}."
    return f"{lead} {facts.property_word} is located in {facts.location}."


def short_specs(listing: Listing, facts: ListingFacts) -> str:
    parts = [p for p in [
        facts.price,
        f"{facts.beds} bd" if facts.beds is not None else None,
        f"{facts.baths} ba" if facts.baths else None,
        f"{facts.sqft:,} sq ft" if facts.sqft else None,
    ] if p]
    return " · ".join(parts)


def highlight_items(facts: ListingFacts) -> list[str]:
    items = list(facts.features)
    if facts.lot:
        items.append(facts.lot.capitalize())
    if facts.year_built:
        items.append(f"Built in {facts.year_built}")
    return items


def hashtags_for(listing: Listing, kind: str, limit: int) -> list[str]:
    tags = []
    kind_tag = {"new_listing": "#NewListing", "price_reduction": "#PriceImprovement", "open_house": "#OpenHouse",
                "back_on_market": "#BackOnMarket", "repost": "#HomeForSale", "status_update": None}.get(kind)
    if kind_tag:
        tags.append(kind_tag)
    if listing.city:
        tags.append(f"#{_tagify(listing.city)}RealEstate")
    if kind in PROMOTIONAL_KINDS and "#HomeForSale" not in tags:
        tags.append("#HomeForSale")
    if listing.city:
        tags.append(f"#{_tagify(listing.city)}Homes")
    tags.append("#RealEstate")
    seen: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.append(tag)
    return seen[: max(limit, 0)]


def call_to_action(kind: str, spec: PlatformSpec, link: str) -> str:
    if kind == "status_update":
        base = "Looking for a home like this one? We can help."
        return f"{base} {link}" if spec.style in ("short", "pin") else f"{base}\nLet's talk: {link}"
    if kind == "open_house":
        lead = "Stop by, or request a private showing"
    else:
        lead = "Interested in learning more or scheduling a showing?"
    if not spec.link_clickable:
        return f"{lead} Tap the link in our bio to view details and request information, or visit {link}"
    if spec.style == "short":
        return f"Details & showings: {link}"
    if spec.style == "pin":
        return "Tap to view details and request a showing."
    if kind == "open_house":
        return f"{lead}: {link}"
    return f"{lead}\nView the property and request more information: {link}"


def _assemble(parts: list[str | None], sep: str = "\n\n") -> str:
    return sep.join(p for p in parts if p)


def render_post(listing: Listing, spec: PlatformSpec, kind: str, link: str, extra: dict[str, Any] | None = None) -> RenderedPost:
    if kind not in POST_KINDS:
        raise ContentError(f"unknown post kind {kind}")
    extra = extra or {}
    facts = listing_facts(listing)
    s = get_settings()
    head = headline(listing, kind, extra)
    hashtags_all = hashtags_for(listing, kind, spec.max_hashtags)
    highlights_all = highlight_items(facts)[:5] if kind != "status_update" else []
    cta = call_to_action(kind, spec, link)
    photos = listing.authorized_photos[: min(s.photos_per_post, spec.max_photos)]
    if kind == "status_update":
        photos = photos[:1]
    address = listing.display_address
    media = [MediaItem(url=p["url"], alt=(p.get("caption") or f"Photo {i + 1} of {address}")[:250]) for i, p in enumerate(photos)]

    def build(n_highlights: int, desc_limit: int | None, n_tags: int) -> tuple[str, str | None]:
        tags = " ".join(hashtags_all[:n_tags]) or None
        desc = excerpt(facts.description, desc_limit) if desc_limit else None
        bullets = "\n".join(f"• {h}" for h in highlights_all[:n_highlights])
        title = None
        if spec.style == "short":
            text = _assemble([head, short_specs(listing, facts) if kind != "status_update" else None, cta,
                              attribution(listing, short=True), tags], sep="\n")
        elif spec.style == "pin":
            title = f"{facts.property_word.title()} for sale in {facts.location}" if kind != "status_update" else head
            if kind in PROMOTIONAL_KINDS and facts.price:
                title = f"{facts.price} {title}"
            title = title[: spec.title_max or 100]
            text = _assemble([summary_sentence(listing, facts, kind, extra),
                              ("Highlights: " + ", ".join(highlights_all[:n_highlights]) + ".") if n_highlights else None,
                              cta, attribution(listing, short=True), tags], sep=" ")
        else:
            text = _assemble([
                head,
                summary_sentence(listing, facts, kind, extra),
                ("Property highlights:\n" + bullets) if n_highlights else None,
                desc,
                cta,
                tags,
                attribution(listing),
            ])
        return text, title

    budget_desc = 600 if spec.style == "long" else 300
    candidates: list[tuple[int, int | None, int]] = []
    for n_h in (min(3, len(highlights_all)), 2, 1, 0):
        if n_h > len(highlights_all):
            continue
        for d in (budget_desc, budget_desc // 2, None):
            candidates.append((n_h, d if spec.style in ("long", "caption") else None, len(hashtags_all)))
    candidates += [(0, None, n) for n in range(len(hashtags_all) - 1, -1, -1)]

    for n_h, d, n_t in candidates:
        text, title = build(n_h, d, n_t)
        if spec.measure(text) <= spec.max_text:
            return RenderedPost(
                platform=spec.platform, kind=kind, text=text, title=title, link=link, media=media,
                hashtags=hashtags_all[:n_t], warnings=list(facts.warnings), requires_review=facts.requires_review,
            )
    raise ContentError(f"required content does not fit {spec.display_name}'s {spec.max_text} character limit")
