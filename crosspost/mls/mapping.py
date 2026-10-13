"""Map RESO Data Dictionary records to :class:`NormalizedListing`.

Shared by the RESO Web API provider and the mock provider (whose fixture uses
RESO field names), so the mock exercises the same mapping as production.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .base import MLSDataError, NormalizedListing, OpenHouse, Photo

STANDARD_STATUSES = {
    "Active", "Active Under Contract", "Canceled", "Closed", "Coming Soon", "Delete", "Expired",
    "Hold", "Incomplete", "Pending", "Withdrawn",
}
# Common non-standard values seen in local MLS feeds.
_STATUS_ALIASES = {
    "a": "Active", "act": "Active", "new": "Active", "active": "Active",
    "u": "Active Under Contract", "contingent": "Active Under Contract", "under contract": "Active Under Contract",
    "p": "Pending", "pend": "Pending", "pending": "Pending",
    "s": "Closed", "sold": "Closed", "closed": "Closed",
    "w": "Withdrawn", "withdrawn": "Withdrawn", "x": "Expired", "expired": "Expired",
    "c": "Canceled", "cancelled": "Canceled", "canceled": "Canceled",
    "coming soon": "Coming Soon", "hold": "Hold", "temp off market": "Hold",
}

FEATURE_FIELDS = (
    "InteriorFeatures", "ExteriorFeatures", "PatioAndPorchFeatures", "PoolFeatures", "Appliances",
    "Flooring", "FireplaceFeatures", "View", "WaterfrontFeatures", "CommunityFeatures",
    "SecurityFeatures", "Cooling", "Heating", "ParkingFeatures", "LotFeatures",
)
# Media permissions that allow redistribution on third-party sites / social media.
PUBLIC_MEDIA_PERMISSIONS = {"public", "idx", "vow", "syndication", "internet"}


def normalize_status(value: Any) -> str:
    if not value:
        return "Unknown"
    text = str(value).strip()
    if text in STANDARD_STATUSES:
        return text
    return _STATUS_ALIASES.get(text.lower(), "Unknown")


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: Any) -> int | None:
    dec = _dec(value)
    return int(dec) if dec is not None else None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "y", "yes"}


def _feature_list(record: dict[str, Any]) -> list[str]:
    features: list[str] = []
    for fld in FEATURE_FIELDS:
        value = record.get(fld)
        if not value:
            continue
        items = value if isinstance(value, list) else str(value).split(",")
        for item in items:
            text = str(item).strip()
            if text and text.lower() not in {"none", "n/a"} and text not in features:
                features.append(text)
    garage = _int(record.get("GarageSpaces"))
    if garage:
        features.append(f"{garage}-car garage")
    if _bool(record.get("PoolPrivateYN"), default=False) and not any("pool" in f.lower() for f in features):
        features.append("Private pool")
    return features


def _photos(record: dict[str, Any]) -> list[Photo]:
    photos: list[Photo] = []
    for i, media in enumerate(record.get("Media") or []):
        url = media.get("MediaURL")
        if not url:
            continue
        category = (media.get("MediaCategory") or "Photo").lower()
        if category not in {"photo", "image"}:
            continue
        permission = media.get("Permission")
        if isinstance(permission, list):
            perms = {str(p).lower() for p in permission}
        elif permission:
            perms = {str(permission).lower()}
        else:
            perms = {"public"}  # absent permission => feed-level license governs
        photos.append(
            Photo(
                url=url,
                order=_int(media.get("Order")) if media.get("Order") is not None else i,
                caption=media.get("ShortDescription") or media.get("LongDescription"),
                authorized=bool(perms & PUBLIC_MEDIA_PERMISSIONS),
            )
        )
    photos.sort(key=lambda p: p.order)
    return photos


def _open_houses(record: dict[str, Any]) -> list[OpenHouse]:
    result: list[OpenHouse] = []
    for oh in record.get("OpenHouse") or record.get("OpenHouses") or []:
        start, end = _dt(oh.get("OpenHouseStartTime")), _dt(oh.get("OpenHouseEndTime"))
        if not start or not end:
            continue
        if str(oh.get("OpenHouseStatus") or "Active").lower() in {"canceled", "cancelled", "ended"}:
            continue
        result.append(
            OpenHouse(
                key=str(oh.get("OpenHouseKey") or oh.get("OpenHouseId") or start.isoformat()),
                start=start,
                end=end,
                remarks=oh.get("OpenHouseRemarks"),
                virtual=str(oh.get("OpenHouseType") or "").lower().startswith("virtual"),
            )
        )
    result.sort(key=lambda o: o.start)
    return result


def reso_to_normalized(record: dict[str, Any]) -> NormalizedListing:
    mls_id = record.get("ListingId") or record.get("ListingKey")
    if not mls_id:
        raise MLSDataError("record missing ListingId/ListingKey")
    street = record.get("UnparsedAddress")
    if not street:
        parts = [record.get("StreetNumber"), record.get("StreetDirPrefix"), record.get("StreetName"),
                 record.get("StreetSuffix"), record.get("StreetDirSuffix")]
        street = " ".join(str(p) for p in parts if p) or None
    agent_name = record.get("ListAgentFullName") or " ".join(
        p for p in [record.get("ListAgentFirstName"), record.get("ListAgentLastName")] if p
    ) or None
    lot_acres = _dec(record.get("LotSizeAcres"))
    lot_sqft = _int(record.get("LotSizeSquareFeet"))
    if lot_sqft is None and lot_acres is not None:
        lot_sqft = int(lot_acres * 43560)
    return NormalizedListing(
        mls_id=str(mls_id),
        listing_key=record.get("ListingKey"),
        status=normalize_status(record.get("StandardStatus") or record.get("MlsStatus")),
        status_raw=record.get("MlsStatus") or record.get("StandardStatus"),
        street_address=street,
        unit_number=record.get("UnitNumber"),
        city=record.get("City"),
        state=record.get("StateOrProvince"),
        postal_code=record.get("PostalCode"),
        address_display_allowed=_bool(record.get("InternetAddressDisplayYN"), True),
        internet_display_allowed=_bool(record.get("InternetEntireListingDisplayYN"), True),
        price=_dec(record.get("ListPrice")),
        original_price=_dec(record.get("OriginalListPrice")),
        property_type=record.get("PropertyType"),
        property_subtype=record.get("PropertySubType"),
        bedrooms=_int(record.get("BedroomsTotal")),
        bathrooms_total=_dec(record.get("BathroomsTotalDecimal") or record.get("BathroomsTotalInteger")),
        bathrooms_full=_int(record.get("BathroomsFull")),
        bathrooms_half=_int(record.get("BathroomsHalf")),
        living_area_sqft=_int(record.get("LivingArea")),
        lot_size_sqft=lot_sqft,
        lot_size_acres=lot_acres,
        year_built=_int(record.get("YearBuilt")),
        description=record.get("PublicRemarks"),
        features=_feature_list(record),
        photos=_photos(record),
        open_houses=_open_houses(record),
        list_agent_id=record.get("ListAgentMlsId") or record.get("ListAgentKey"),
        list_agent_name=agent_name,
        list_agent_phone=record.get("ListAgentDirectPhone") or record.get("ListAgentPreferredPhone"),
        list_agent_email=record.get("ListAgentEmail"),
        list_office_id=record.get("ListOfficeMlsId") or record.get("ListOfficeKey"),
        list_office_name=record.get("ListOfficeName"),
        list_office_phone=record.get("ListOfficePhone"),
        listing_url=record.get("ListingURL"),
        created_at=_dt(record.get("OriginalEntryTimestamp") or record.get("ListingContractDate")),
        modified_at=_dt(record.get("ModificationTimestamp")),
        raw=record,
    )
