"""Record reconciliation helpers for package inbox storage."""

from __future__ import annotations

from typing import Any

from .carrier_rules import normalize_carrier
from .const import (
    STATUS_DELIVERED,
    STATUS_EXPECTED_TODAY,
    STATUS_IN_TRANSIT,
    STATUS_PICKED_UP,
    STATUS_READY_FOR_PICKUP,
    STATUS_UNKNOWN,
)
from .parser import clean_text, stable_key


TRACKING_DIAGNOSTIC_FIELDS = {
    "tracking_url",
    "tracking_api_url",
    "tracking_refresh_url",
    "tracking_last_checked",
    "tracking_refresh_source",
    "tracking_refresh_error",
    "tracking_refresh_supported",
    "tracking_refresh_has_delivery_detail",
}


def apply_vinted_cross_reference(
    record: dict[str, Any],
    packages: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Merge Vinted source-of-truth details into the referenced carrier record."""
    reference = carrier_tracking_reference(record)
    if _carrier_slug(record.get("carrier")) == "vinted" and reference:
        carrier_record = _carrier_record_from_vinted_reference(record, reference)
        key = stable_key(carrier_record)
        existing = packages.get(key)
        if isinstance(existing, dict):
            return _merge_vinted_into_carrier(existing, record, reference)
        return carrier_record

    linked_vinted = _linked_vinted_record(record, packages)
    if linked_vinted:
        linked_reference = carrier_tracking_reference(linked_vinted)
        if linked_reference:
            return _merge_vinted_into_carrier(record, linked_vinted, linked_reference)

    return record


def carrier_tracking_reference(record: dict[str, Any]) -> dict[str, str] | None:
    """Return a normalized carrier tracking reference embedded in record.extra."""
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    reference = extra.get("carrier_tracking")
    if not isinstance(reference, dict):
        return None

    carrier = normalize_carrier(str(reference.get("carrier") or ""))
    tracking_code = clean_text(str(reference.get("tracking_code") or "")).upper()
    if carrier == "unknown" or not tracking_code:
        return None

    result = {
        "carrier": carrier,
        "tracking_code": tracking_code,
    }
    tracking_url = clean_text(str(reference.get("tracking_url") or ""))
    if tracking_url:
        result["tracking_url"] = tracking_url
    return result


def merge_tracking_update(
    record: dict[str, Any],
    update: dict[str, Any],
    checked_at: str,
) -> dict[str, Any]:
    """Merge a tracking update while preserving stronger Vinted/pickup state."""
    if _should_preserve_fulfillment_state(record, update):
        return _merge_tracking_diagnostics_only(record, update, checked_at)

    merged = dict(record)
    carrier = _carrier_slug(record.get("carrier") or update.get("carrier"))
    for key, value in update.items():
        if value is None:
            continue
        if key == "tracking_url" and record.get("tracking_url"):
            continue
        if key == "extra" and isinstance(value, dict):
            previous_extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            merged["extra"] = {**previous_extra, **value}
            continue
        if key == "confidence" and _confidence_rank(value) < _confidence_rank(record.get("confidence")):
            continue
        if key == "status" and value == STATUS_UNKNOWN and record.get("status") != STATUS_UNKNOWN:
            continue
        merged[key] = value

    merged["tracking_last_checked"] = checked_at
    merged["tracking_refresh_has_delivery_detail"] = _tracking_update_has_delivery_detail(update)
    if (
        carrier == "chronopost"
        and update.get("status") in (STATUS_IN_TRANSIT, STATUS_EXPECTED_TODAY, STATUS_DELIVERED)
        and not update.get("pickup_location")
    ):
        merged["pickup_location"] = None
        merged["pickup_code"] = None
    if carrier == "chronopost" and update.get("status") == STATUS_IN_TRANSIT:
        merged["expected_date"] = None
        merged["delivery_window_start"] = None
        merged["delivery_window_end"] = None
    if merged.get("status") in (STATUS_DELIVERED, STATUS_PICKED_UP, "cancelled"):
        merged["expected_date"] = None
        merged["delivery_window_start"] = None
        merged["delivery_window_end"] = None
        merged["pickup_location"] = None
        merged["pickup_code"] = None
    elif merged.get("status") == STATUS_READY_FOR_PICKUP:
        merged["delivery_window_start"] = None
        merged["delivery_window_end"] = None
    if not update.get("tracking_refresh_error"):
        merged.pop("tracking_refresh_error", None)
    return merged


def _carrier_record_from_vinted_reference(
    record: dict[str, Any],
    reference: dict[str, str],
) -> dict[str, Any]:
    merged = dict(record)
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    merged["carrier"] = reference["carrier"]
    merged["tracking_code"] = reference["tracking_code"]
    merged["tracking_url"] = reference.get("tracking_url") or record.get("tracking_url")
    merged["shop"] = record.get("shop") or "Vinted"
    merged["source"] = "vinted_cross_reference"
    merged["confidence"] = "high"
    merged["extra"] = {
        **extra,
        "carrier_tracking": reference,
        "vinted_cross_reference": _vinted_reference_snapshot(record),
    }
    merged.pop("key", None)
    return merged


def _merge_vinted_into_carrier(
    carrier_record: dict[str, Any],
    vinted_record: dict[str, Any],
    reference: dict[str, str],
) -> dict[str, Any]:
    merged = dict(carrier_record)
    extra = carrier_record.get("extra") if isinstance(carrier_record.get("extra"), dict) else {}
    vinted_extra = vinted_record.get("extra") if isinstance(vinted_record.get("extra"), dict) else {}
    merged["carrier"] = reference["carrier"]
    merged["tracking_code"] = reference["tracking_code"]
    if reference.get("tracking_url") and not merged.get("tracking_url"):
        merged["tracking_url"] = reference["tracking_url"]
    if _generic_carrier_shop(merged.get("shop")) and vinted_record.get("shop"):
        merged["shop"] = vinted_record["shop"]

    vinted_status = str(vinted_record.get("status") or STATUS_UNKNOWN)
    if vinted_status != STATUS_UNKNOWN and _status_rank(vinted_status) >= _status_rank(merged.get("status")):
        merged["status"] = vinted_status

    for key in (
        "expected_date",
        "delivery_window_start",
        "delivery_window_end",
        "pickup_location",
        "pickup_code",
        "qr_file_path",
    ):
        if vinted_record.get(key):
            merged[key] = vinted_record[key]

    if vinted_record.get("tracking_status_text") and not merged.get("tracking_status_text"):
        merged["tracking_status_text"] = vinted_record["tracking_status_text"]
    if _confidence_rank(vinted_record.get("confidence")) > _confidence_rank(merged.get("confidence")):
        merged["confidence"] = vinted_record["confidence"]
    for key in ("message_id", "imap_uid", "raw_excerpt"):
        if vinted_record.get(key) and not merged.get(key):
            merged[key] = vinted_record[key]

    merged["source"] = "vinted_cross_reference"
    merged["extra"] = {
        **extra,
        **vinted_extra,
        "carrier_tracking": reference,
        "vinted_cross_reference": _vinted_reference_snapshot(vinted_record),
    }
    return merged


def _linked_vinted_record(
    record: dict[str, Any],
    packages: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    carrier = _carrier_slug(record.get("carrier"))
    tracking_code = clean_text(str(record.get("tracking_code") or "")).upper()
    if carrier == "unknown" or not tracking_code:
        return None
    for candidate in packages.values():
        if not isinstance(candidate, dict) or _carrier_slug(candidate.get("carrier")) != "vinted":
            continue
        reference = carrier_tracking_reference(candidate)
        if not reference:
            continue
        if reference["carrier"] == carrier and reference["tracking_code"] == tracking_code:
            return candidate
    return None


def _vinted_reference_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "key",
            "status",
            "expected_date",
            "delivery_window_start",
            "delivery_window_end",
            "pickup_location",
            "pickup_code",
            "qr_file_path",
            "source",
            "updated_at",
            "created_at",
        )
        if record.get(key)
    }


def _merge_tracking_diagnostics_only(
    record: dict[str, Any],
    update: dict[str, Any],
    checked_at: str,
) -> dict[str, Any]:
    merged = dict(record)
    for key in TRACKING_DIAGNOSTIC_FIELDS:
        if key == "tracking_url" and record.get("tracking_url"):
            continue
        if key in update and update[key] is not None:
            merged[key] = update[key]
    merged["tracking_last_checked"] = checked_at
    merged["tracking_refresh_has_delivery_detail"] = False
    if update.get("tracking_refresh_error"):
        merged["tracking_refresh_error"] = update["tracking_refresh_error"]
    else:
        merged.pop("tracking_refresh_error", None)
    return merged


def _should_preserve_fulfillment_state(record: dict[str, Any], update: dict[str, Any]) -> bool:
    carrier = _carrier_slug(record.get("carrier") or update.get("carrier"))
    if carrier != "chronopost":
        return False
    if not _record_has_strong_fulfillment_state(record):
        return False
    if update.get("status") not in (STATUS_IN_TRANSIT, STATUS_EXPECTED_TODAY, STATUS_UNKNOWN, None):
        return False
    return not _update_has_strong_fulfillment_detail(update)


def _record_has_strong_fulfillment_state(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    source = clean_text(str(record.get("source") or "")).lower()
    if extra.get("vinted_cross_reference") or source.startswith("vinted"):
        return record.get("status") in (STATUS_READY_FOR_PICKUP, STATUS_DELIVERED, STATUS_PICKED_UP)
    if source.startswith("manual_correction"):
        return record.get("status") in (STATUS_READY_FOR_PICKUP, STATUS_DELIVERED, STATUS_PICKED_UP)
    return bool(record.get("pickup_location") and record.get("status") == STATUS_READY_FOR_PICKUP)


def _update_has_strong_fulfillment_detail(update: dict[str, Any]) -> bool:
    status = update.get("status")
    if status in (STATUS_READY_FOR_PICKUP, STATUS_DELIVERED, STATUS_PICKED_UP):
        return True
    return bool(
        update.get("pickup_location")
        or update.get("pickup_code")
        or update.get("delivery_window_start")
        and update.get("delivery_window_end")
    )


def _tracking_update_has_delivery_detail(update: dict[str, Any]) -> bool:
    return bool(
        update.get("expected_date")
        or update.get("delivery_window_start")
        or update.get("delivery_window_end")
        or update.get("pickup_location")
        or update.get("status") not in (None, STATUS_UNKNOWN)
    )


def _carrier_slug(value: Any) -> str:
    text = clean_text(str(value or "unknown")).lower()
    if text in {"vinted", "amazon", "apotheek"}:
        return text
    return normalize_carrier(text)


def _confidence_rank(value: Any) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(clean_text(str(value or "")).lower(), 0)


def _status_rank(value: Any) -> int:
    return {
        STATUS_UNKNOWN: 0,
        STATUS_IN_TRANSIT: 10,
        STATUS_EXPECTED_TODAY: 20,
        STATUS_READY_FOR_PICKUP: 30,
        STATUS_DELIVERED: 40,
        STATUS_PICKED_UP: 50,
        "cancelled": 50,
    }.get(str(value or STATUS_UNKNOWN), 0)


def _generic_carrier_shop(value: Any) -> bool:
    text = clean_text(str(value or "")).lower()
    return text in {"", "pakket", "package", "chronopost", "vinted go"}
