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

VINTED_CARRIER_SLUGS = {
    "chronopost",
    "dhl",
    "dpd",
    "gls",
    "homerr",
    "postnl",
    "ups",
}

VINTED_DETAIL_EXTRA_KEYS = {
    "expected_date_end",
    "tracking_events",
    "vinted_expected_date_to",
    "vinted_id",
    "vinted_item_title",
    "vinted_other_party",
    "vinted_thread_id",
    "vinted_tracking_code",
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

    if _carrier_slug(record.get("carrier")) == "vinted":
        inferred = _probable_carrier_reference_for_vinted_record(record, packages)
        if inferred:
            carrier_record = _carrier_record_from_vinted_reference(record, inferred)
            key = stable_key(carrier_record)
            existing = packages.get(key)
            merged = (
                _merge_vinted_into_carrier(existing, record, inferred)
                if isinstance(existing, dict)
                else carrier_record
            )
            _mark_vinted_auto_link(merged, inferred, reason="single_vinted_carrier_candidate")
            return merged

    linked_vinted = _linked_vinted_record(record, packages)
    if linked_vinted:
        linked_reference = carrier_tracking_reference(linked_vinted)
        if linked_reference:
            return _merge_vinted_into_carrier(record, linked_vinted, linked_reference)

    probable_vinted = _probable_vinted_record_for_carrier(record, packages)
    if probable_vinted:
        inferred = _carrier_reference_from_carrier_record(record)
        if inferred:
            merged = _merge_vinted_into_carrier(record, probable_vinted, inferred)
            _mark_vinted_auto_link(merged, inferred, reason="stored_vinted_candidate")
            return merged

    return record


def reconcile_vinted_carrier_links(packages: dict[str, dict[str, Any]]) -> list[str]:
    """Collapse high-confidence Vinted/carrier duplicates already in storage."""
    changed: list[str] = []
    for key, record in list(packages.items()):
        if key not in packages or not isinstance(record, dict):
            continue
        if _carrier_slug(record.get("carrier")) != "vinted":
            continue

        other_packages = {other_key: value for other_key, value in packages.items() if other_key != key}
        merged = apply_vinted_cross_reference(record, other_packages)
        if _carrier_slug(merged.get("carrier")) == "vinted":
            continue

        new_key = merged.get("key") or stable_key(merged)
        merged["key"] = new_key
        if new_key != key:
            packages.pop(key, None)
        packages[new_key] = merged
        changed.append(new_key)
    return changed


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
    if not merged.get("expected_date"):
        vinted_expected = _vinted_expected_start(merged)
        if vinted_expected:
            merged["expected_date"] = vinted_expected
    if (
        carrier == "chronopost"
        and update.get("status") == STATUS_IN_TRANSIT
        and not _record_has_vinted_expected_detail(merged)
    ):
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
    merged["source"] = "vinted_sidecar_cross_reference" if _is_authoritative_vinted_record(record) else "vinted_cross_reference"
    merged["confidence"] = "high"
    merged["extra"] = {
        **extra,
        "carrier_tracking": reference,
        "vinted_cross_reference": _vinted_reference_snapshot(record),
    }
    if _is_authoritative_vinted_record(record):
        _clear_stale_fulfillment_fields(merged)
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

    authoritative = _is_authoritative_vinted_record(vinted_record)
    vinted_status = str(vinted_record.get("status") or STATUS_UNKNOWN)
    applied_vinted_status = _should_apply_vinted_status(vinted_status, merged.get("status"), authoritative=authoritative)
    if applied_vinted_status:
        merged["status"] = vinted_status

    for key in (
        "expected_date",
        "delivery_window_start",
        "delivery_window_end",
        "qr_file_path",
    ):
        if vinted_record.get(key) and (authoritative or applied_vinted_status or not merged.get(key)):
            merged[key] = vinted_record[key]
    for key in ("pickup_location", "pickup_code"):
        if vinted_record.get(key) and (
            authoritative or (applied_vinted_status and merged.get("status") == STATUS_READY_FOR_PICKUP)
        ):
            merged[key] = vinted_record[key]

    if vinted_record.get("tracking_status_text") and not merged.get("tracking_status_text"):
        merged["tracking_status_text"] = vinted_record["tracking_status_text"]
    if _confidence_rank(vinted_record.get("confidence")) > _confidence_rank(merged.get("confidence")):
        merged["confidence"] = vinted_record["confidence"]
    for key in ("message_id", "imap_uid", "raw_excerpt"):
        if vinted_record.get(key) and not merged.get(key):
            merged[key] = vinted_record[key]

    merged["source"] = "vinted_sidecar_cross_reference" if authoritative else "vinted_cross_reference"
    merged["extra"] = {
        **extra,
        **vinted_extra,
        "carrier_tracking": reference,
        "vinted_cross_reference": _vinted_reference_snapshot(vinted_record),
    }
    if authoritative:
        _clear_stale_fulfillment_fields(merged)
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


def _probable_carrier_reference_for_vinted_record(
    record: dict[str, Any],
    packages: dict[str, dict[str, Any]],
) -> dict[str, str] | None:
    if not _is_vinted_record(record) or not _vinted_record_has_linkable_detail(record):
        return None

    strong_matches: list[dict[str, Any]] = []
    context_matches: list[dict[str, Any]] = []
    for candidate in packages.values():
        if not isinstance(candidate, dict) or not _is_vinted_carrier_candidate(candidate):
            continue
        if not _vinted_link_status_compatible(record, candidate):
            continue
        if _vinted_records_share_platform_id(record, candidate) or _same_tracking_code(record, candidate):
            strong_matches.append(candidate)
            continue
        if _record_has_vinted_context(candidate) and _carrier_record_needs_vinted_detail(candidate):
            context_matches.append(candidate)

    matches = strong_matches or context_matches
    if len(matches) != 1:
        return None
    return _carrier_reference_from_carrier_record(matches[0])


def _probable_vinted_record_for_carrier(
    record: dict[str, Any],
    packages: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not _is_vinted_carrier_candidate(record) or not _record_has_vinted_context(record):
        return None

    matches: list[dict[str, Any]] = []
    for candidate in packages.values():
        if not isinstance(candidate, dict) or not _is_vinted_record(candidate):
            continue
        if carrier_tracking_reference(candidate):
            continue
        if not _vinted_record_has_linkable_detail(candidate):
            continue
        if not _vinted_link_status_compatible(candidate, record):
            continue
        if _vinted_records_share_platform_id(candidate, record) or _same_tracking_code(candidate, record):
            return candidate
        if _carrier_record_needs_vinted_detail(record):
            matches.append(candidate)

    return matches[0] if len(matches) == 1 else None


def _carrier_reference_from_carrier_record(record: dict[str, Any]) -> dict[str, str] | None:
    carrier = _carrier_slug(record.get("carrier"))
    tracking_code = clean_text(str(record.get("tracking_code") or "")).upper()
    if carrier not in VINTED_CARRIER_SLUGS or not tracking_code:
        return None
    reference = {"carrier": carrier, "tracking_code": tracking_code}
    tracking_url = clean_text(str(record.get("tracking_url") or ""))
    if tracking_url:
        reference["tracking_url"] = tracking_url
    return reference


def _is_vinted_record(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return (
        _carrier_slug(record.get("carrier")) == "vinted"
        or clean_text(str(record.get("shop") or "")).lower() == "vinted"
        or clean_text(str(record.get("source") or "")).lower().startswith("vinted")
        or bool(extra.get("vinted_cross_reference"))
        or any(extra.get(key) for key in VINTED_DETAIL_EXTRA_KEYS)
    )


def _is_vinted_carrier_candidate(record: dict[str, Any]) -> bool:
    carrier = _carrier_slug(record.get("carrier"))
    tracking_code = clean_text(str(record.get("tracking_code") or ""))
    return carrier in VINTED_CARRIER_SLUGS and bool(tracking_code)


def _record_has_vinted_context(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return (
        clean_text(str(record.get("shop") or "")).lower() == "vinted"
        or clean_text(str(record.get("source") or "")).lower().startswith("vinted")
        or bool(extra.get("vinted_cross_reference"))
        or any(extra.get(key) for key in VINTED_DETAIL_EXTRA_KEYS)
    )


def _record_has_vinted_expected_detail(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    cross_reference = extra.get("vinted_cross_reference")
    cross_reference = cross_reference if isinstance(cross_reference, dict) else {}
    return bool(
        _record_has_vinted_context(record)
        and (
            record.get("expected_date")
            or extra.get("expected_date_end")
            or extra.get("vinted_expected_date_to")
            or cross_reference.get("expected_date")
            or cross_reference.get("expected_date_end")
            or cross_reference.get("vinted_expected_date_to")
        )
    )


def _vinted_expected_start(record: dict[str, Any]) -> Any:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    cross_reference = extra.get("vinted_cross_reference")
    cross_reference = cross_reference if isinstance(cross_reference, dict) else {}
    return cross_reference.get("expected_date") or extra.get("vinted_expected_date_from")


def _vinted_record_has_linkable_detail(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    if any(extra.get(key) for key in VINTED_DETAIL_EXTRA_KEYS):
        return True
    return bool(record.get("expected_date") or record.get("pickup_code") or record.get("pickup_location"))


def _carrier_record_needs_vinted_detail(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return not any(extra.get(key) for key in ("vinted_item_title", "vinted_other_party", "tracking_events"))


def _same_tracking_code(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_code = clean_text(str(left.get("tracking_code") or "")).upper()
    right_code = clean_text(str(right.get("tracking_code") or "")).upper()
    return bool(left_code and right_code and left_code == right_code)


def _vinted_records_share_platform_id(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_ids = _vinted_platform_ids(left)
    right_ids = _vinted_platform_ids(right)
    return bool(left_ids and right_ids and left_ids & right_ids)


def _vinted_platform_ids(record: dict[str, Any]) -> set[str]:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    values = {
        record.get("tracking_code"),
        extra.get("vinted_id"),
        extra.get("vinted_tracking_code"),
        extra.get("vinted_thread_id"),
    }
    cross_reference = extra.get("vinted_cross_reference")
    if isinstance(cross_reference, dict):
        values.update(
            {
                cross_reference.get("tracking_code"),
                cross_reference.get("vinted_id"),
                cross_reference.get("vinted_tracking_code"),
                cross_reference.get("vinted_thread_id"),
            }
        )
    return {clean_text(str(value)).upper() for value in values if clean_text(str(value or ""))}


def _vinted_link_status_compatible(vinted_record: dict[str, Any], carrier_record: dict[str, Any]) -> bool:
    vinted_status = str(vinted_record.get("status") or STATUS_UNKNOWN)
    carrier_status = str(carrier_record.get("status") or STATUS_UNKNOWN)
    if STATUS_DELIVERED in {vinted_status, carrier_status}:
        return vinted_status == carrier_status
    if STATUS_PICKED_UP in {vinted_status, carrier_status}:
        return vinted_status == carrier_status
    return True


def _mark_vinted_auto_link(record: dict[str, Any], reference: dict[str, str], *, reason: str) -> None:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    record["extra"] = {
        **extra,
        "vinted_auto_link": {
            "carrier": reference["carrier"],
            "tracking_code": reference["tracking_code"],
            "reason": reason,
        },
    }


def _vinted_reference_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        key: record.get(key)
        for key in (
            "key",
            "tracking_code",
            "tracking_url",
            "status",
            "expected_date",
            "delivery_window_start",
            "delivery_window_end",
            "pickup_location",
            "pickup_code",
            "qr_file_path",
            "tracking_status_text",
            "source",
            "updated_at",
            "created_at",
        )
        if record.get(key)
    }
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    for key in VINTED_DETAIL_EXTRA_KEYS:
        if extra.get(key):
            snapshot[key] = extra[key]
    return snapshot


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
    if source.startswith("vinted_sidecar"):
        return record.get("status") in (STATUS_READY_FOR_PICKUP, STATUS_DELIVERED, STATUS_PICKED_UP)
    if source.startswith("vinted") or extra.get("vinted_cross_reference"):
        return False
    if source.startswith("manual_correction"):
        return record.get("status") in (STATUS_READY_FOR_PICKUP, STATUS_DELIVERED, STATUS_PICKED_UP)
    return bool(record.get("pickup_location") and record.get("status") == STATUS_READY_FOR_PICKUP)


def _is_authoritative_vinted_record(record: dict[str, Any]) -> bool:
    source = clean_text(str(record.get("source") or "")).lower()
    return source.startswith("vinted_sidecar")


def _should_apply_vinted_status(
    vinted_status: str,
    current_status: Any,
    *,
    authoritative: bool,
) -> bool:
    if vinted_status == STATUS_UNKNOWN:
        return False
    if authoritative:
        return True
    current = str(current_status or STATUS_UNKNOWN)
    if current == STATUS_UNKNOWN:
        return True
    if vinted_status == STATUS_READY_FOR_PICKUP and current not in (STATUS_READY_FOR_PICKUP, STATUS_UNKNOWN):
        return False
    if vinted_status in (STATUS_DELIVERED, STATUS_PICKED_UP, "cancelled"):
        return _status_rank(vinted_status) >= _status_rank(current)
    return _status_rank(vinted_status) > _status_rank(current)


def _clear_stale_fulfillment_fields(record: dict[str, Any]) -> None:
    status = record.get("status")
    if status in (STATUS_DELIVERED, STATUS_PICKED_UP, "cancelled"):
        record["expected_date"] = None
        record["delivery_window_start"] = None
        record["delivery_window_end"] = None
        record["pickup_location"] = None
        record["pickup_code"] = None
        return
    if status == STATUS_READY_FOR_PICKUP:
        record["expected_date"] = None
        record["delivery_window_start"] = None
        record["delivery_window_end"] = None
        return
    if status != STATUS_READY_FOR_PICKUP:
        record["pickup_location"] = None
        record["pickup_code"] = None


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
