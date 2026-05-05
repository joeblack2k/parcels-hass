"""Delivery-window calculations for Parcels."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from .const import (
    STATUS_DELIVERED,
    STATUS_PICKED_UP,
    STATUS_READY_FOR_PICKUP,
)

DEFAULT_WINDOW_MARGIN_MINUTES = 30
TERMINAL_STATUSES = {STATUS_DELIVERED, STATUS_PICKED_UP, STATUS_READY_FOR_PICKUP, "cancelled"}


def build_delivery_snapshot(
    records: list[dict[str, Any]],
    *,
    now: datetime,
    margin_minutes: int = DEFAULT_WINDOW_MARGIN_MINUTES,
) -> dict[str, Any]:
    """Build a package delivery-window snapshot for sensors and automations."""
    margin = timedelta(minutes=margin_minutes)
    delivery_records = dedupe_delivery_records(
        [record for record in records if record.get("status") not in TERMINAL_STATUSES]
    )
    windows = [
        window
        for record in delivery_records
        if (window := _window_for_record(record, now=now)) is not None
    ]
    windows.sort(key=lambda item: item["window_start_dt"])

    active_packages: list[dict[str, Any]] = []
    for window in windows:
        margin_start = window["window_start_dt"] - margin
        margin_end = window["window_end_dt"] + margin
        in_margin = margin_start <= now <= margin_end
        in_window = window["window_start_dt"] <= now <= window["window_end_dt"]
        if in_margin:
            active_packages.append(
                {
                    **_public_record(window["record"]),
                    "window_start": window["window_start_dt"].isoformat(),
                    "window_end": window["window_end_dt"].isoformat(),
                    "in_window": in_window,
                    "in_margin": in_margin,
                }
            )

    next_window = next(
        (
            window
            for window in windows
            if window["window_end_dt"] + margin >= now
        ),
        None,
    )

    if any(package["in_window"] for package in active_packages):
        weight = 3
        reason = "inside_delivery_window"
    elif active_packages:
        weight = 2
        reason = "near_delivery_window"
    elif delivery_records:
        weight = 1
        reason = "delivery_expected_today"
    else:
        weight = 0
        reason = "no_delivery_expected"

    return {
        "active": bool(active_packages),
        "weight": weight,
        "reason": reason,
        "expected_today_count": len(delivery_records),
        "window_count": len(windows),
        "active_packages": active_packages,
        "packages": [_public_record(record) for record in delivery_records],
        "windows": [_public_window(window) for window in windows],
        "next_window": _public_window(next_window) if next_window else None,
        "margin_minutes": margin_minutes,
    }


def _window_for_record(record: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    expected_date = record.get("expected_date")
    start, end = _valid_window_values(record)
    if not expected_date or not start or not end:
        return None

    try:
        start_dt = datetime.combine(
            datetime.strptime(str(expected_date), "%Y-%m-%d").date(),
            time.fromisoformat(str(start)),
            tzinfo=now.tzinfo,
        )
        end_dt = datetime.combine(
            datetime.strptime(str(expected_date), "%Y-%m-%d").date(),
            time.fromisoformat(str(end)),
            tzinfo=now.tzinfo,
        )
    except ValueError:
        return None

    if end_dt < start_dt:
        end_dt += timedelta(days=1)
    return {
        "record": record,
        "window_start_dt": start_dt,
        "window_end_dt": end_dt,
    }


def dedupe_delivery_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe records while keeping separate tracked parcels distinct."""
    tracked: dict[tuple[Any, ...], dict[str, Any]] = {}
    tracked_windows: dict[tuple[Any, ...], dict[str, Any]] = {}
    untracked: dict[tuple[Any, ...], dict[str, Any]] = {}

    for record in records:
        tracking_code = record.get("tracking_code")
        if tracking_code:
            key = ("tracking", record.get("carrier"), str(tracking_code).lower())
            tracked[key] = _prefer_record(tracked.get(key), record)
            if window_key := _window_key(record):
                tracked_windows[window_key] = tracked[key]

    for record in records:
        if record.get("tracking_code"):
            continue
        if (window_key := _window_key(record)) and window_key in tracked_windows:
            continue
        key = _window_key(record) or (
            "summary",
            record.get("carrier"),
            _normal_text(record.get("shop")),
            record.get("expected_date"),
            record.get("status"),
        )
        untracked[key] = _prefer_record(untracked.get(key), record)

    return [*tracked.values(), *untracked.values()]


def _window_key(record: dict[str, Any]) -> tuple[Any, ...] | None:
    start, end = _valid_window_values(record)
    if not start or not end:
        return None
    return (
        "window",
        record.get("carrier"),
        _normal_text(record.get("shop")),
        record.get("expected_date"),
        start,
        end,
    )


def _prefer_record(existing: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return candidate
    existing_score = _record_score(existing)
    candidate_score = _record_score(candidate)
    return candidate if candidate_score > existing_score else existing


def _record_score(record: dict[str, Any]) -> tuple[int, int, int]:
    return (
        1 if record.get("tracking_code") else 0,
        1 if record.get("tracking_status_text") else 0,
        1 if record.get("tracking_url") else 0,
    )


def _normal_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    start, end = _valid_window_values(record)
    return {
        "key": record.get("key"),
        "carrier": record.get("carrier"),
        "shop": record.get("shop"),
        "status": record.get("status"),
        "expected_date": record.get("expected_date"),
        "delivery_window_start": start,
        "delivery_window_end": end,
        "tracking_code": record.get("tracking_code"),
        "tracking_url": record.get("tracking_url"),
        "tracking_status_text": record.get("tracking_status_text"),
        "source": record.get("source"),
        "confidence": record.get("confidence"),
    }


def _valid_window_values(record: dict[str, Any]) -> tuple[str | None, str | None]:
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
    return (str(start), str(end))


def _public_window(window: dict[str, Any] | None) -> dict[str, Any] | None:
    if not window:
        return None
    record = window["record"]
    return {
        **_public_record(record),
        "window_start": window["window_start_dt"].isoformat(),
        "window_end": window["window_end_dt"].isoformat(),
    }
