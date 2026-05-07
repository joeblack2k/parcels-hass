"""Notification text formatting for package inbox."""

from __future__ import annotations

from datetime import datetime
import hashlib
import re
from typing import Any

from .carrier_rules import normalize_carrier
from .parser import clean_text


UNKNOWN = "onbekend"


def format_pickup_notification(record: dict[str, Any]) -> str:
    """Format the one-off pickup notification for a package record."""
    if is_vinted_record(record):
        return format_vinted_pickup_notification(record)

    carrier = carrier_title(record.get("carrier"))
    location = clean_text(str(record.get("pickup_location") or ""))
    code = clean_text(str(record.get("pickup_code") or ""))
    shop = clean_text(str(record.get("shop") or ""))
    display = notification_package_title(record)

    first_line = f"{display} pakket ligt klaar"
    if location:
        first_line += f" bij {location}"

    lines = [first_line]
    if carrier.lower() != display.lower():
        lines.append(f"Vervoerder: {carrier}")
    if shop and shop.lower() not in {carrier.lower(), display.lower()}:
        lines.append(f"Van: {shop}")
    if code:
        lines.append(f"Code: {code}")

    return "\n\n".join([lines[0], "\n".join(lines[1:])]) if len(lines) > 1 else lines[0]


def format_pickup_summary(records: list[dict[str, Any]]) -> str:
    """Format the manual pickup summary for all outstanding pickup records."""
    count = len(records)
    first = (
        "Er ligt 1 pakket klaar om op te halen"
        if count == 1
        else f"Er liggen {count} pakketten klaar om op te halen"
    )

    lines = [first]
    for record in records:
        if is_vinted_record(record):
            details = vinted_pickup_details(record)
            lines.append("")
            lines.append(f"- Vinted: {details['article']}")
            lines.append(f"  Winkel: {details['store']}")
            lines.append(f"  Adres: {details['address']}")
            lines.append(f"  Code: {details['code']}")
            lines.append(f"  Ophalen tot: {details['deadline']}")
            if record.get("qr_file_path"):
                lines.append("  QR: al meegestuurd")
            continue

        shop = record.get("shop") or carrier_title(record.get("carrier"))
        location = record.get("pickup_location")
        code = record.get("pickup_code")
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        deadline = extra.get("pickup_deadline") or record.get("pickup_deadline")

        lines.append("")
        lines.append(f"- {shop}")
        if location:
            lines.append(f"  Bij: {location}")
        if code:
            lines.append(f"  Code: {code}")
        if deadline:
            lines.append(f"  Ophalen voor: {deadline}")
        if record.get("qr_file_path"):
            lines.append("  QR: al meegestuurd")
    return "\n".join(lines)


def format_vinted_pickup_notification(record: dict[str, Any]) -> str:
    """Format Vinted pickup notifications with the fields that matter at the door."""
    details = vinted_pickup_details(record)
    return "\n".join(
        (
            "Vinted pakket ligt klaar!",
            f"Artikel: {details['article']}",
            f"Winkel: {details['store']}",
            f"Adres: {details['address']}",
            f"Code: {details['code']}",
            f"Ophalen tot: {details['deadline']}",
        )
    )


def format_vinted_tracking_notification(record: dict[str, Any]) -> str:
    """Format an in-room Vinted tracking update with the app-visible details."""
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    article = first_clean(extra.get("vinted_item_title"), record.get("item_title"), record.get("title")) or UNKNOWN
    other_party = first_clean(extra.get("vinted_other_party"), extra.get("seller"), extra.get("buyer"))
    carrier = vinted_tracking_carrier(record)
    tracking_code = vinted_tracking_code(record)
    expected = vinted_expected_text(record)
    link = vinted_tracking_link(record)
    status = vinted_status_label(record.get("status"))
    events = vinted_tracking_events(record)[:4]

    lines = [f"Vinted pakket {status}", f"Artikel: {article}"]
    if other_party:
        lines.append(f"Vinted: {other_party}")
    if carrier:
        lines.append(f"Vervoerder: {carrier}")
    if tracking_code:
        lines.append(f"Trackingcode: {tracking_code}")
    if expected:
        lines.append(f"Verwacht: {expected}")
    if link:
        lines.append(f"Tracking: {link}")
    if events:
        lines.append("")
        lines.append("Trackinginformatie:")
        for event in events:
            label = first_clean(event.get("status"), event.get("label"), event.get("message"))
            timestamp = format_event_timestamp(event.get("timestamp") or event.get("created_at") or event.get("date"))
            if label and timestamp:
                lines.append(f"- {label}: {timestamp}")
            elif label:
                lines.append(f"- {label}")
    return "\n".join(lines)


def vinted_tracking_fingerprint(record: dict[str, Any]) -> str:
    """Return a compact fingerprint for deduping Vinted tracking room updates."""
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    cross = extra.get("vinted_cross_reference") if isinstance(extra.get("vinted_cross_reference"), dict) else {}
    events = vinted_tracking_events(record)[:4]
    parts = [
        str(record.get("status") or ""),
        str(record.get("tracking_code") or ""),
        str(record.get("expected_date") or cross.get("expected_date") or ""),
        str(
            extra.get("expected_date_end")
            or extra.get("vinted_expected_date_to")
            or cross.get("expected_date_end")
            or cross.get("vinted_expected_date_to")
            or ""
        ),
        str(vinted_tracking_code(record) or ""),
        str(vinted_tracking_link(record) or ""),
        "|".join(
            f"{event.get('status') or event.get('label') or event.get('message')}/{event.get('timestamp') or event.get('created_at') or event.get('date')}"
            for event in events
            if isinstance(event, dict)
        ),
    ]
    raw = "\n".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def should_notify_vinted_tracking(record: dict[str, Any]) -> bool:
    if not is_vinted_record(record):
        return False
    status = str(record.get("status") or "")
    if status in {"", "unknown", "ready_for_pickup", "picked_up", "cancelled", "canceled"}:
        return False
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return bool(
        first_clean(extra.get("vinted_item_title"), record.get("item_title"), record.get("title"))
        and (
            vinted_tracking_code(record)
            or vinted_tracking_link(record)
            or record.get("expected_date")
            or extra.get("tracking_events")
        )
    )


def vinted_pickup_details(record: dict[str, Any]) -> dict[str, str]:
    """Return display-safe Vinted pickup fields."""
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    location = first_clean(
        record.get("pickup_location"),
        extra.get("pickup_location"),
        extra.get("vinted_pickup_location"),
        extra.get("pickup_point"),
        extra.get("vinted_pickup_point"),
    )
    store, address = split_pickup_location(location)

    store = first_clean(
        extra.get("pickup_shop"),
        extra.get("pickup_store"),
        extra.get("pickup_point_name"),
        extra.get("vinted_pickup_point_name"),
        store,
    )
    address = first_clean(
        extra.get("pickup_address"),
        extra.get("vinted_pickup_address"),
        address,
    )
    article = first_clean(
        extra.get("vinted_item_title"),
        record.get("item_title"),
        record.get("title"),
    )
    code = first_clean(
        record.get("pickup_code"),
        extra.get("pickup_code"),
        extra.get("vinted_pickup_code"),
        extra.get("collection_code"),
    )
    deadline = first_clean(
        extra.get("pickup_deadline"),
        record.get("pickup_deadline"),
        extra.get("pickup_until"),
        extra.get("vinted_pickup_until"),
        extra.get("collection_deadline"),
    )

    return {
        "article": article or UNKNOWN,
        "store": store or UNKNOWN,
        "address": address or UNKNOWN,
        "code": code or UNKNOWN,
        "deadline": deadline or UNKNOWN,
    }


def notification_package_title(record: dict[str, Any]) -> str:
    """Return the best short package label for generic notifications."""
    if is_vinted_record(record):
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        title = first_clean(extra.get("vinted_item_title"), record.get("item_title"), record.get("title"))
        return title or "Vinted"
    shop = clean_text(str(record.get("shop") or ""))
    if shop:
        return shop
    return carrier_title(record.get("carrier"))


def is_vinted_record(record: dict[str, Any]) -> bool:
    """Return true when a record came from Vinted or was enriched by Vinted."""
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return (
        carrier_slug(record.get("carrier")) == "vinted"
        or clean_text(str(record.get("shop") or "")).lower() == "vinted"
        or clean_text(str(record.get("source") or "")).lower().startswith("vinted")
        or bool(extra.get("vinted_cross_reference"))
        or bool(extra.get("vinted_item_title"))
    )


def vinted_tracking_carrier(record: dict[str, Any]) -> str:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    reference = extra.get("carrier_tracking") if isinstance(extra.get("carrier_tracking"), dict) else {}
    carrier = reference.get("carrier") or record.get("carrier")
    if carrier_slug(carrier) == "vinted":
        return ""
    return carrier_title(carrier)


def vinted_tracking_code(record: dict[str, Any]) -> str:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    reference = extra.get("carrier_tracking") if isinstance(extra.get("carrier_tracking"), dict) else {}
    cross = extra.get("vinted_cross_reference") if isinstance(extra.get("vinted_cross_reference"), dict) else {}
    return first_clean(
        reference.get("tracking_code"),
        record.get("tracking_code") if carrier_slug(record.get("carrier")) != "vinted" else None,
        extra.get("vinted_tracking_code"),
        cross.get("tracking_code"),
        record.get("tracking_code"),
    )


def vinted_tracking_link(record: dict[str, Any]) -> str:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    reference = extra.get("carrier_tracking") if isinstance(extra.get("carrier_tracking"), dict) else {}
    return first_clean(
        reference.get("tracking_url"),
        extra.get("vinted_tracking_page_url"),
        record.get("tracking_url"),
    )


def vinted_expected_text(record: dict[str, Any]) -> str:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    cross = extra.get("vinted_cross_reference") if isinstance(extra.get("vinted_cross_reference"), dict) else {}
    start = first_clean(record.get("expected_date"), cross.get("expected_date"))
    end = first_clean(
        extra.get("expected_date_end"),
        extra.get("vinted_expected_date_to"),
        cross.get("expected_date_end"),
        cross.get("vinted_expected_date_to"),
    )
    if start and end and start != end:
        return f"{format_date(start)} - {format_date(end)}"
    if start:
        return format_date(start)
    return ""


def vinted_tracking_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    events = extra.get("tracking_events")
    if isinstance(events, list):
        return [event for event in events if isinstance(event, dict)]
    cross = extra.get("vinted_cross_reference") if isinstance(extra.get("vinted_cross_reference"), dict) else {}
    cross_events = cross.get("tracking_events")
    return [event for event in cross_events if isinstance(event, dict)] if isinstance(cross_events, list) else []


def vinted_status_label(value: Any) -> str:
    status = str(value or "").replace("_", " ")
    return {
        "in transit": "is onderweg",
        "expected today": "komt vandaag",
        "delivered": "is afgeleverd",
    }.get(status, status or "is bijgewerkt")


MONTHS = {
    1: "januari",
    2: "februari",
    3: "maart",
    4: "april",
    5: "mei",
    6: "juni",
    7: "juli",
    8: "augustus",
    9: "september",
    10: "oktober",
    11: "november",
    12: "december",
}


def format_date(value: Any) -> str:
    text = first_clean(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return f"{parsed.day} {MONTHS.get(parsed.month, parsed.strftime('%m'))}"


def format_event_timestamp(value: Any) -> str:
    text = first_clean(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return f"{parsed.day} {MONTHS.get(parsed.month, parsed.strftime('%m'))} {parsed.year} {parsed.strftime('%H:%M')}"


def split_pickup_location(location: str) -> tuple[str, str]:
    """Split a combined pickup point into store and address when possible."""
    text = clean_text(str(location or ""))
    if not text:
        return ("", "")

    comma_parts = [part.strip(" ,") for part in text.split(",") if part.strip(" ,")]
    if len(comma_parts) >= 2:
        return (comma_parts[0], ", ".join(comma_parts[1:]))

    dash_parts = [part.strip(" -") for part in re.split(r"\s+-\s+", text) if part.strip(" -")]
    if len(dash_parts) >= 2 and looks_like_address(" ".join(dash_parts[1:])):
        return (dash_parts[0], " - ".join(dash_parts[1:]))

    match = re.match(
        r"(?P<store>.+?)\s+(?P<address>(?:[A-ZÀ-ÿ][A-Za-zÀ-ÿ' -]{2,}"
        r"(?:straat|laan|weg|plein|dijk|kade|kamp|hof|pad|singel|steeg|gracht|dam|markt|plantsoen)"
        r"\s+\d+[A-Za-z]?(?:\s+.*)?))$",
        text,
        re.IGNORECASE,
    )
    if match:
        return (match.group("store").strip(" ,.-"), match.group("address").strip(" ,.-"))

    return (text, "")


def looks_like_address(value: str) -> bool:
    text = clean_text(str(value or ""))
    return bool(re.search(r"\d", text) and re.search(r"(straat|laan|weg|plein|dijk|kade|hof|pad|dam)", text, re.I))


def first_clean(*values: Any) -> str:
    for value in values:
        text = clean_text(str(value or ""))
        if text:
            return text
    return ""


def carrier_title(value: Any) -> str:
    carrier = carrier_slug(value)
    return {
        "postnl": "PostNL",
        "dhl": "DHL",
        "dpd": "DPD",
        "gls": "GLS",
        "fedex": "FedEx",
        "chronopost": "Chronopost",
        "ups": "UPS",
        "trunkrs": "Trunkrs",
        "homerr": "Homerr",
        "cycloon": "Cycloon",
        "instabox": "Instabox",
        "transmission": "TransMission",
        "dachser": "Dachser",
        "dynalogic": "Dynalogic",
        "gofo": "GOFO Express",
        "dragonfly": "Dragonfly",
        "amazon": "Amazon",
        "vinted": "Vinted",
        "apotheek": "Apotheek",
        "unknown": "Pakket",
    }.get(carrier, str(value or "Pakket"))


def carrier_slug(value: Any) -> str:
    carrier = normalize_carrier(str(value or "unknown"))
    return carrier or "unknown"
