"""Dashboard snapshot helpers for Parcels."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from .const import (
    STATUS_DELIVERED,
    STATUS_EXPECTED_TODAY,
    STATUS_IN_TRANSIT,
    STATUS_PICKED_UP,
    STATUS_READY_FOR_PICKUP,
    STATUS_UNKNOWN,
)

TERMINAL_STATUSES = {STATUS_DELIVERED, STATUS_PICKED_UP, "cancelled"}
_MONTHS_NL = {
    1: "jan",
    2: "feb",
    3: "mrt",
    4: "apr",
    5: "mei",
    6: "jun",
    7: "jul",
    8: "aug",
    9: "sep",
    10: "okt",
    11: "nov",
    12: "dec",
}


def build_dashboard_snapshot(
    records: list[dict[str, Any]],
    *,
    delivery_snapshot: dict[str, Any] | None = None,
    now: datetime,
    history_limit: int = 30,
) -> dict[str, Any]:
    """Build a compact but complete dashboard payload from package records."""
    today = now.date().isoformat()
    deduped = _dedupe_records(records)

    active: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for record in deduped:
        public = _public_dashboard_record(record, today=today)
        if _is_active_record(record, today=today):
            active.append(public)
        else:
            history.append(public)

    active.sort(key=_active_sort_key)
    history.sort(key=_history_sort_key, reverse=True)

    active_count = len(active)
    history_total = len(history)
    today_count = sum(1 for record in active if _record_due_today(record, today=today))
    pickup_count = sum(1 for record in active if record.get("status") == STATUS_READY_FOR_PICKUP)

    snapshot = delivery_snapshot or {}
    window_weight = int(snapshot.get("weight") or 0)
    active_windows = snapshot.get("active_packages") if isinstance(snapshot.get("active_packages"), list) else []

    return {
        "active": active,
        "history": history[:history_limit],
        "counts": {
            "active": active_count,
            "today": today_count,
            "pickup": pickup_count,
            "in_delivery_window": len(active_windows),
            "history": history_total,
        },
        "updated_at": now.isoformat(),
        "next_delivery_window": snapshot.get("next_window"),
        "delivery_window_weight": window_weight,
    }


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        key = _dedupe_key(record)
        deduped[key] = _prefer_record(deduped.get(key), record)
    return list(deduped.values())


def _dedupe_key(record: dict[str, Any]) -> tuple[Any, ...]:
    carrier = _normal_text(record.get("carrier"))
    tracking_code = _normal_text(record.get("tracking_code"))
    if tracking_code:
        return ("tracking", carrier, tracking_code)

    pickup_code = _normal_text(record.get("pickup_code"))
    pickup_location = _normal_text(record.get("pickup_location"))
    if pickup_code or pickup_location:
        return (
            "pickup",
            carrier,
            _normal_text(record.get("shop")),
            pickup_code,
            pickup_location,
        )

    key = _normal_text(record.get("key"))
    if key:
        return ("key", key)

    return (
        "summary",
        carrier,
        _normal_text(record.get("shop")),
        record.get("expected_date") or "",
        record.get("delivery_window_start") or "",
        record.get("delivery_window_end") or "",
        record.get("status") or STATUS_UNKNOWN,
    )


def _prefer_record(existing: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return candidate
    return candidate if _record_score(candidate) >= _record_score(existing) else existing


def _record_score(record: dict[str, Any]) -> tuple[int, int, float]:
    status = str(record.get("status") or STATUS_UNKNOWN)
    status_score = {
        STATUS_DELIVERED: 50,
        STATUS_PICKED_UP: 50,
        "cancelled": 50,
        STATUS_READY_FOR_PICKUP: 40,
        STATUS_EXPECTED_TODAY: 30,
        STATUS_IN_TRANSIT: 20,
        STATUS_UNKNOWN: 10,
    }.get(status, 10)
    detail_score = sum(
        1
        for key in (
            "tracking_status_text",
            "tracking_url",
            "expected_date",
            "delivery_window_start",
            "delivery_window_end",
            "pickup_location",
            "pickup_code",
            "qr_file_path",
            "extra",
        )
        if record.get(key)
    )
    return (status_score, detail_score, _timestamp(record.get("updated_at") or record.get("created_at")))


def _is_active_record(record: dict[str, Any], *, today: str) -> bool:
    status = str(record.get("status") or STATUS_UNKNOWN)
    if status in TERMINAL_STATUSES:
        return False
    if status == STATUS_READY_FOR_PICKUP:
        return True

    expected_date = _date_from_value(record.get("expected_date"))
    if expected_date:
        return expected_date >= today

    if record.get("tracking_code"):
        return True

    return status in (STATUS_EXPECTED_TODAY, STATUS_IN_TRANSIT)


def _record_due_today(record: dict[str, Any], *, today: str) -> bool:
    status = str(record.get("status") or STATUS_UNKNOWN)
    if status == STATUS_READY_FOR_PICKUP:
        return False
    expected_date = _date_from_value(record.get("expected_date"))
    return expected_date == today or status == STATUS_EXPECTED_TODAY


def _public_dashboard_record(record: dict[str, Any], *, today: str) -> dict[str, Any]:
    status = str(record.get("status") or STATUS_UNKNOWN)
    carrier = str(record.get("carrier") or "unknown")
    shop = record.get("shop") or _carrier_title(carrier)
    window_start, window_end = _valid_window_values(record)
    item_title = _parcel_title(record)
    other_party = _parcel_other_party(record)
    expected_date_end = _expected_date_end(record)
    return {
        "key": record.get("key"),
        "carrier": carrier,
        "carrier_title": _carrier_title(carrier),
        "shop": shop,
        "status": status,
        "status_label": _status_label(status),
        "expected_date": record.get("expected_date"),
        "expected_date_end": expected_date_end,
        "expected_date_label": _expected_date_label(record),
        "delivery_window_start": window_start,
        "delivery_window_end": window_end,
        "pickup_location": record.get("pickup_location"),
        "pickup_code": record.get("pickup_code"),
        "qr_file_path": record.get("qr_file_path"),
        "has_qr": bool(record.get("qr_file_path")),
        "tracking_code": record.get("tracking_code"),
        "tracking_url": record.get("tracking_url"),
        "tracking_status_text": record.get("tracking_status_text"),
        "tracking_events": _tracking_events(record),
        "tracking_last_checked": record.get("tracking_last_checked"),
        "tracking_refresh_url": record.get("tracking_refresh_url"),
        "tracking_refresh_source": record.get("tracking_refresh_source"),
        "tracking_refresh_error": record.get("tracking_refresh_error"),
        "tracking_refresh_supported": record.get("tracking_refresh_supported"),
        "tracking_refresh_has_delivery_detail": record.get("tracking_refresh_has_delivery_detail"),
        "source": record.get("source"),
        "confidence": record.get("confidence"),
        "message_id": record.get("message_id"),
        "imap_uid": record.get("imap_uid"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "parcel_title": item_title,
        "item_title": item_title,
        "other_party": other_party,
        "vinted_other_party": other_party if _is_vinted_record(record) else None,
        "is_terminal": status in TERMINAL_STATUSES,
        "due_today": _record_due_today(record, today=today),
        "display_title": _display_title(record, shop),
        "display_subtitle": _display_subtitle(record),
    }


def _display_title(record: dict[str, Any], fallback: Any) -> str:
    title = _parcel_title(record)
    if _is_vinted_record(record) and title:
        return title
    return str(fallback or _carrier_title(record.get("carrier")))


def _display_subtitle(record: dict[str, Any]) -> str:
    status = str(record.get("status") or STATUS_UNKNOWN)
    carrier = _display_carrier_title(record)
    if status == STATUS_READY_FOR_PICKUP:
        location = record.get("pickup_location")
        return f"{carrier} afhalen bij {location}" if location else f"{carrier} afhalen"

    expected_date = record.get("expected_date")
    start, end = _valid_window_values(record)
    if _is_vinted_record(record):
        parts = [carrier]
        other_party = _parcel_other_party(record)
        expected = _expected_date_label(record)
        if other_party:
            parts.append(f"via {other_party}")
        if expected:
            parts.append(f"verwacht {expected}")
        elif record.get("tracking_status_text"):
            parts.append(str(record["tracking_status_text"]))
        elif status != STATUS_UNKNOWN:
            parts.append(_status_label(status))
        return " - ".join(parts)
    if expected_date and start and end:
        return f"{carrier} {expected_date} {start}-{end}"
    if expected_date:
        return f"{carrier} {expected_date}"
    return record.get("tracking_status_text") or _status_label(status)


def _display_carrier_title(record: dict[str, Any]) -> str:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    shop = str(record.get("shop") or "")
    source = str(record.get("source") or "").lower()
    if shop.lower() == "vinted" or source.startswith("vinted") or extra.get("vinted_cross_reference"):
        return "Vinted"
    return _carrier_title(record.get("carrier"))


def _is_vinted_record(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    shop = str(record.get("shop") or "").lower()
    source = str(record.get("source") or "").lower()
    return (
        str(record.get("carrier") or "").lower() == "vinted"
        or shop == "vinted"
        or source.startswith("vinted")
        or bool(extra.get("vinted_cross_reference"))
        or bool(extra.get("vinted_item_title"))
    )


def _parcel_title(record: dict[str, Any]) -> str | None:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    for key in ("parcel_title", "item_title", "vinted_item_title"):
        value = _clean_public_text(record.get(key) or extra.get(key))
        if value:
            return value
    cross_reference = extra.get("vinted_cross_reference")
    if isinstance(cross_reference, dict):
        for key in ("parcel_title", "item_title", "vinted_item_title"):
            value = _clean_public_text(cross_reference.get(key))
            if value:
                return value
    return None


def _parcel_other_party(record: dict[str, Any]) -> str | None:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    for key in ("other_party", "seller", "vinted_other_party"):
        value = _clean_public_text(record.get(key) or extra.get(key))
        if value:
            return value
    cross_reference = extra.get("vinted_cross_reference")
    if isinstance(cross_reference, dict):
        for key in ("other_party", "seller", "vinted_other_party"):
            value = _clean_public_text(cross_reference.get(key))
            if value:
                return value
    return None


def _expected_date_end(record: dict[str, Any]) -> str | None:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    value = (
        record.get("expected_date_end")
        or extra.get("expected_date_end")
        or extra.get("vinted_expected_date_to")
    )
    return _date_from_value(value)


def _expected_date_label(record: dict[str, Any]) -> str | None:
    start = _date_from_value(record.get("expected_date"))
    end = _expected_date_end(record)
    if start and end and start != end:
        return _date_range_label(start, end)
    if start:
        return _date_label(start) if _is_vinted_record(record) else start
    return None


def _tracking_events(record: dict[str, Any]) -> list[dict[str, str]]:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    events = extra.get("tracking_events")
    if not isinstance(events, list):
        return []
    public: list[dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        status = _clean_public_text(event.get("status"))
        timestamp = _clean_public_text(event.get("timestamp") or event.get("date"))
        if not status or not timestamp:
            continue
        item = {"status": status[:120], "timestamp": timestamp[:40]}
        location = _clean_public_text(event.get("location"))
        if location:
            item["location"] = location[:120]
        tracking_code = _clean_public_text(event.get("tracking_code"))
        if tracking_code:
            item["tracking_code"] = tracking_code[:80]
        public.append(item)
        if len(public) >= 10:
            break
    return public


def _date_range_label(start: str, end: str) -> str:
    start_date = _parse_iso_date(start)
    end_date = _parse_iso_date(end)
    if not start_date or not end_date:
        return f"{start} t/m {end}"
    if start_date.year == end_date.year and start_date.month == end_date.month:
        return f"{start_date.day}-{end_date.day} {_MONTHS_NL[start_date.month]}"
    return f"{_date_label(start)} - {_date_label(end)}"


def _date_label(value: str) -> str:
    parsed = _parse_iso_date(value)
    if not parsed:
        return value
    return f"{parsed.day} {_MONTHS_NL[parsed.month]}"


def _parse_iso_date(value: Any) -> date | None:
    text = _date_from_value(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _clean_public_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    if not text or text.lower() in {"unknown", "onbekend", "none", "null"}:
        return None
    return text


def _active_sort_key(record: dict[str, Any]) -> tuple[int, str, str, str]:
    status = str(record.get("status") or STATUS_UNKNOWN)
    priority = 0 if status == STATUS_READY_FOR_PICKUP else 1
    expected_date = _date_from_value(record.get("expected_date")) or "9999-12-31"
    start = str(_valid_window_values(record)[0] or "99:99")
    title = _normal_text(record.get("shop") or record.get("carrier"))
    return (priority, expected_date, start, title)


def _history_sort_key(record: dict[str, Any]) -> tuple[str, float, str]:
    expected_date = _date_from_value(record.get("expected_date")) or ""
    updated = _timestamp(record.get("updated_at") or record.get("created_at"))
    title = _normal_text(record.get("shop") or record.get("carrier"))
    return (expected_date, updated, title)


def _date_from_value(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _valid_window_values(record: dict[str, Any]) -> tuple[Any | None, Any | None]:
    start = record.get("delivery_window_start")
    end = record.get("delivery_window_end")
    if not start or not end:
        return (None, None)
    try:
        start_time = time.fromisoformat(str(start))
        end_time = time.fromisoformat(str(end))
    except ValueError:
        return (None, None)
    if start_time == end_time:
        return (None, None)
    return (start, end)


def _timestamp(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _normal_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _carrier_title(value: Any) -> str:
    carrier = _normal_text(value)
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


def _status_label(status: Any) -> str:
    return {
        STATUS_DELIVERED: "Bezorgd",
        STATUS_EXPECTED_TODAY: "Vandaag verwacht",
        STATUS_IN_TRANSIT: "Onderweg",
        STATUS_PICKED_UP: "Opgehaald",
        STATUS_READY_FOR_PICKUP: "Afhalen",
        "cancelled": "Geannuleerd",
        STATUS_UNKNOWN: "Onbekend",
    }.get(str(status or STATUS_UNKNOWN), str(status or STATUS_UNKNOWN))
