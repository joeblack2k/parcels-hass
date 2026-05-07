"""Suppression helpers for imports the user explicitly deleted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .parser import clean_text


DELETED_IMPORT_RETENTION_DAYS = 365


def deleted_import_tombstone_for_record(
    package_key: str,
    record: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a tombstone that prevents a deleted Vinted import coming back."""
    if not is_vinted_import_record(record):
        return None
    identifiers = sorted(vinted_import_identifiers(record, package_key=package_key))
    if not identifiers:
        return None
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    cross = extra.get("vinted_cross_reference") if isinstance(extra.get("vinted_cross_reference"), dict) else {}
    reference = extra.get("carrier_tracking") if isinstance(extra.get("carrier_tracking"), dict) else {}
    timestamp = now or datetime.now(timezone.utc)
    return {
        "deleted_at": timestamp.isoformat(),
        "package_key": package_key,
        "carrier": record.get("carrier"),
        "tracking_code": record.get("tracking_code"),
        "item_title": extra.get("vinted_item_title") or cross.get("vinted_item_title"),
        "thread_id": extra.get("vinted_thread_id") or cross.get("vinted_thread_id"),
        "carrier_tracking_code": reference.get("tracking_code"),
        "identifiers": identifiers,
    }


def record_matches_deleted_import(record: dict[str, Any], deleted_imports: Any) -> bool:
    """Return true when a Vinted import matches a user-deleted tombstone."""
    if not is_vinted_import_record(record) or not isinstance(deleted_imports, dict):
        return False
    identifiers = vinted_import_identifiers(record, package_key=clean_text(str(record.get("key") or "")))
    if not identifiers:
        return False
    entries = deleted_imports.get("vinted")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tombstone_ids = {
            clean_identifier(value)
            for value in entry.get("identifiers", [])
            if clean_identifier(value)
        }
        if identifiers & tombstone_ids:
            return True
    return False


def prune_deleted_imports(
    deleted_imports: Any,
    *,
    now: datetime | None = None,
    retention_days: int = DELETED_IMPORT_RETENTION_DAYS,
) -> None:
    """Drop expired/invalid tombstones in place."""
    if not isinstance(deleted_imports, dict):
        return
    entries = deleted_imports.get("vinted")
    if not isinstance(entries, list):
        deleted_imports["vinted"] = []
        return

    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=retention_days)
    kept: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifiers = entry.get("identifiers")
        if not isinstance(identifiers, list) or not any(clean_identifier(value) for value in identifiers):
            continue
        deleted_at = parse_datetime(entry.get("deleted_at"))
        if deleted_at and deleted_at < cutoff:
            continue
        kept.append(entry)
    deleted_imports["vinted"] = kept


def is_vinted_import_record(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    source = clean_text(str(record.get("source") or "")).lower()
    shop = clean_text(str(record.get("shop") or "")).lower()
    carrier = clean_text(str(record.get("carrier") or "")).lower()
    return (
        carrier == "vinted"
        or shop == "vinted"
        or source.startswith("vinted")
        or bool(extra.get("vinted_cross_reference"))
        or bool(extra.get("vinted_item_title"))
        or bool(extra.get("vinted_thread_id"))
        or bool(extra.get("vinted_id"))
    )


def vinted_import_identifiers(record: dict[str, Any], *, package_key: str | None = None) -> set[str]:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    cross = extra.get("vinted_cross_reference") if isinstance(extra.get("vinted_cross_reference"), dict) else {}
    reference = extra.get("carrier_tracking") if isinstance(extra.get("carrier_tracking"), dict) else {}
    values = [
        package_key,
        record.get("key"),
        record.get("tracking_code"),
        record.get("tracking_url"),
        extra.get("vinted_id"),
        extra.get("vinted_tracking_code"),
        extra.get("vinted_thread_id"),
        extra.get("vinted_source_url"),
        reference.get("tracking_code"),
        reference.get("tracking_url"),
        cross.get("key"),
        cross.get("tracking_code"),
        cross.get("tracking_url"),
        cross.get("vinted_id"),
        cross.get("vinted_tracking_code"),
        cross.get("vinted_thread_id"),
    ]
    return {identifier for value in values if (identifier := clean_identifier(value))}


def clean_identifier(value: Any) -> str:
    return clean_text(str(value or "")).lower()


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
