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
    return {
        "key": record.get("key"),
        "carrier": carrier,
        "carrier_title": _carrier_title(carrier),
        "shop": shop,
        "status": status,
        "status_label": _status_label(status),
        "expected_date": record.get("expected_date"),
        "delivery_window_start": window_start,
        "delivery_window_end": window_end,
        "pickup_location": record.get("pickup_location"),
        "pickup_code": record.get("pickup_code"),
        "qr_file_path": record.get("qr_file_path"),
        "has_qr": bool(record.get("qr_file_path")),
        "tracking_code": record.get("tracking_code"),
        "tracking_url": record.get("tracking_url"),
        "tracking_status_text": record.get("tracking_status_text"),
        "tracking_last_checked": record.get("tracking_last_checked"),
        "tracking_refresh_source": record.get("tracking_refresh_source"),
        "tracking_refresh_error": record.get("tracking_refresh_error"),
        "tracking_refresh_supported": record.get("tracking_refresh_supported"),
        "source": record.get("source"),
        "confidence": record.get("confidence"),
        "message_id": record.get("message_id"),
        "imap_uid": record.get("imap_uid"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "is_terminal": status in TERMINAL_STATUSES,
        "due_today": _record_due_today(record, today=today),
        "display_title": shop,
        "display_subtitle": _display_subtitle(record),
    }


def _display_subtitle(record: dict[str, Any]) -> str:
    status = str(record.get("status") or STATUS_UNKNOWN)
    carrier = _carrier_title(record.get("carrier"))
    if status == STATUS_READY_FOR_PICKUP:
        location = record.get("pickup_location")
        return f"{carrier} afhalen bij {location}" if location else f"{carrier} afhalen"

    expected_date = record.get("expected_date")
    start, end = _valid_window_values(record)
    if expected_date and start and end:
        return f"{carrier} {expected_date} {start}-{end}"
    if expected_date:
        return f"{carrier} {expected_date}"
    return record.get("tracking_status_text") or _status_label(status)


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
