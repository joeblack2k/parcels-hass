"""Best-effort public track-and-trace enrichment.

Carrier pages change often and some require postcode/account checks. This module
only extracts obvious information from public HTML/embedded JSON and reports a
soft error when a page cannot be interpreted.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from typing import Any
from urllib.parse import urlencode, quote_plus

from .carrier_rules import normalize_carrier
from .const import (
    STATUS_DELIVERED,
    STATUS_EXPECTED_TODAY,
    STATUS_IN_TRANSIT,
    STATUS_READY_FOR_PICKUP,
    STATUS_UNKNOWN,
)
from .parser import clean_text


NORMALIZED_TRACKING_STATUSES = {
    STATUS_DELIVERED,
    STATUS_EXPECTED_TODAY,
    STATUS_IN_TRANSIT,
    STATUS_READY_FOR_PICKUP,
    STATUS_UNKNOWN,
}

PUBLIC_TRACKING_CARRIERS = {
    "postnl",
    "dhl",
    "dpd",
    "gls",
    "fedex",
    "chronopost",
    "ups",
    "trunkrs",
    "homerr",
    "cycloon",
    "instabox",
    "transmission",
    "dachser",
    "dynalogic",
    "gofo",
    "dragonfly",
}

TRACKING_URLS = {
    "postnl": "https://www.postnl.nl/tracktrace/?B={code}",
    "dhl": "https://www.dhl.com/nl-nl/home/tracking.html?tracking-id={code}",
    "dpd": "https://www.dpd.com/nl/nl/ontvangen/volgen/?parcelNumber={code}",
    "gls": "https://www.gls-info.nl/Tracking?match={code}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={code}",
    "chronopost": "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT={code}",
    "ups": "https://www.ups.com/track?tracknum={code}",
    "trunkrs": "https://parcel.trunkrs.nl/",
    "homerr": "https://vintedgo.com/tracking",
    "cycloon": "https://www.cycloon.eu/trackandtrace",
    "instabox": "https://tracking.instabox.io/",
    "transmission": "https://www.trans-mission.nl/track-trace/",
    "dachser": "https://www.dachser.com/nl/en/tracking",
    "dynalogic": "https://track.dynalogic.eu/",
    "gofo": "https://www.gofoexpress.com/track",
    "dragonfly": "https://www.dragonflyshipping.com/track",
}

BLOCKED_HINTS = (
    "captcha",
    "robot",
    "bot detection",
    "access denied",
    "access-denied",
    "toegang geweigerd",
    "permission to view this webpage",
    "don't have permission",
    "do not have permission",
    "can't process your request",
    "cannot process your request",
    "unable to process your request",
    "system down",
    "system-error",
    "page not found",
    "currently not available",
    "errors.edgesuite.net",
)

HUMAN_REQUIRED_HINTS = (
    "postcode",
    "postalcode",
    "zip code",
    "postal code",
    "huisnummer",
    "house number",
    "log in",
    "login",
    "sign in",
)

TRACKING_BLOCKED_ERROR = "tracking_page_blocked_or_permission"

def build_tracking_url(
    carrier: str | None,
    tracking_code: str | None,
    *,
    delivery_postcode: str | None = None,
    delivery_house_number: str | None = None,
) -> str | None:
    """Build the public tracking URL for a carrier/code pair."""
    carrier_slug = normalize_carrier(carrier)
    code = (tracking_code or "").strip()
    postcode = _normalize_postcode(delivery_postcode)
    house_number = _normalize_house_number(delivery_house_number)
    if carrier_slug == "postnl" and code and postcode:
        return "https://www.postnl.nl/tracktrace/?" + urlencode(
            {
                "B": code,
                "P": postcode,
                "D": "NL",
            }
        )
    if carrier_slug == "dhl" and code.upper().startswith("JJD"):
        return f"https://my.dhlecommerce.nl/go-track-trace?role=consumer-receiver&tc={quote_plus(code)}"
    if carrier_slug == "ups" and code:
        return TRACKING_URLS["ups"].format(code=quote_plus(code))
    if carrier_slug == "trunkrs":
        if code and postcode:
            return f"https://parcel.trunkrs.nl/{quote_plus(code)}/{quote_plus(postcode)}"
        return TRACKING_URLS["trunkrs"]
    if carrier_slug == "dynalogic":
        query = _tracking_query(
            {
                "tracking": code,
                "postalCode": postcode,
                "houseNumber": house_number,
            }
        )
        return f"{TRACKING_URLS['dynalogic']}?{query}" if query else TRACKING_URLS["dynalogic"]
    if carrier_slug in {"homerr", "cycloon", "instabox", "transmission", "dachser", "gofo", "dragonfly"}:
        query = _tracking_query(
            {
                "tracking": code,
                "postalCode": postcode,
                "houseNumber": house_number,
            }
        )
        return f"{TRACKING_URLS[carrier_slug]}?{query}" if query else TRACKING_URLS[carrier_slug]
    template = TRACKING_URLS.get(carrier_slug)
    if not template or not code:
        return None
    return template.format(code=quote_plus(code))


def build_tracking_api_url(
    carrier: str | None,
    tracking_code: str | None,
    *,
    delivery_postcode: str | None = None,
    delivery_house_number: str | None = None,
) -> str | None:
    """Build a public JSON endpoint when a carrier exposes one without login."""
    carrier_slug = normalize_carrier(carrier)
    code = (tracking_code or "").strip()
    postcode = _normalize_postcode(delivery_postcode)
    if carrier_slug == "dhl" and code:
        key = f"{code}+{postcode}" if postcode else code
        if not code.upper().startswith("JJD"):
            return f"https://api-gw.dhlparcel.nl/track-trace?key={quote_plus(key)}"
        return (
            "https://my.dhlecommerce.nl/receiver-parcel-api/track-trace"
            f"?key={quote_plus(key)}&role=consumer-receiver"
        )
    return None


def build_fedex_tracking_api_url() -> str:
    """Return the public FedEx tracking endpoint used by the web tracker."""

    return "https://www.fedex.com/track/v2/shipments"


def supports_public_tracking(carrier: str | None) -> bool:
    """Return whether this carrier has a public page adapter."""
    return normalize_carrier(carrier) in PUBLIC_TRACKING_CARRIERS


def normalize_tracking_scraper_update(
    payload: Any,
    *,
    carrier: str,
    tracking_code: str,
    tracking_url: str | None = None,
    today: date | None = None,
) -> dict[str, Any] | None:
    """Normalize a local scraper sidecar response into integration fields."""
    if not isinstance(payload, dict):
        return None
    today = today or date.today()
    carrier_slug = normalize_carrier(payload.get("carrier") or carrier)
    code = clean_text(str(payload.get("tracking_code") or tracking_code))
    if not code:
        return None

    update: dict[str, Any] = {
        "carrier": carrier_slug,
        "tracking_code": code,
        "tracking_url": payload.get("tracking_url") or tracking_url or build_tracking_url(carrier_slug, code),
        "tracking_refresh_source": payload.get("tracking_refresh_source") or "local_tracking_scraper",
        "tracking_refresh_supported": True,
    }
    if payload.get("tracking_api_url"):
        update["tracking_api_url"] = clean_text(str(payload["tracking_api_url"]))
    if payload.get("tracking_refresh_url"):
        update["tracking_refresh_url"] = clean_text(str(payload["tracking_refresh_url"]))

    if payload.get("tracking_refresh_error"):
        update["tracking_refresh_error"] = clean_text(str(payload["tracking_refresh_error"]))

    expected_date = _date_from_scraper_value(
        payload.get("expected_date")
        or payload.get("estimated_delivery")
        or payload.get("delivery_date"),
        today,
    )
    if expected_date:
        update["expected_date"] = expected_date

    start, end = _scraper_window(payload)
    if start and end:
        update["delivery_window_start"] = start
        update["delivery_window_end"] = end

    raw_status = clean_text(
        str(
            payload.get("raw_status")
            or payload.get("tracking_status_text")
            or payload.get("status_text")
            or payload.get("status")
            or ""
        )
    )
    status_text = clean_text(str(payload.get("tracking_status_text") or payload.get("status_text") or raw_status))
    if status_text:
        update["tracking_status_text"] = status_text[:220]

    update["status"] = _status_from_scraper_payload(payload.get("status"), raw_status, expected_date, today)

    location = clean_text(str(payload.get("location") or ""))
    if location and update.get("tracking_status_text") and location.lower() not in str(update["tracking_status_text"]).lower():
        update["tracking_status_text"] = f"{update['tracking_status_text']} - {location}"[:220]

    events = payload.get("events")
    if isinstance(events, list) and events:
        update["extra"] = {"tracking_events": events[:20]}
    return update


def _tracking_query(values: dict[str, str | None]) -> str:
    return urlencode({key: value for key, value in values.items() if value})


def _normalize_postcode(value: str | None) -> str | None:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return text or None


def _normalize_house_number(value: str | None) -> str | None:
    text = re.sub(r"\s+", "", str(value or "").strip())
    return text or None


def _date_from_scraper_value(value: Any, today: date) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    parsed = _datetime_from_value(value)
    if parsed:
        return parsed.date().isoformat()
    return _extract_expected_date(str(value), today)


def _scraper_window(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    start = _time_from_any(payload.get("delivery_window_start") or payload.get("window_start"))
    end = _time_from_any(payload.get("delivery_window_end") or payload.get("window_end"))
    if start and end and start != end:
        return (start, end)
    timeframe = payload.get("delivery_timeframe") or payload.get("delivery_window")
    return _extract_time_window(str(timeframe or ""))


def _status_from_scraper_payload(
    status: Any,
    raw_status: str,
    expected_date: str | None,
    today: date,
) -> str:
    status_slug = re.sub(r"[^a-z0-9]+", "_", str(status or "").lower()).strip("_")
    if status_slug in NORMALIZED_TRACKING_STATUSES:
        return status_slug
    text = clean_text(f"{status or ''} {raw_status}").lower()
    if any(term in text for term in ("delivered", "afgeleverd", "bezorgd", "delivre", "delivree")):
        return STATUS_DELIVERED
    if any(term in text for term in ("out_for_delivery", "out for delivery", "on fedex vehicle for delivery")):
        return STATUS_EXPECTED_TODAY
    if any(term in text for term in ("exception", "failed", "failure", "clearance delay")):
        return STATUS_UNKNOWN
    inferred = _extract_status(text, expected_date, today)
    if inferred != STATUS_UNKNOWN:
        return inferred
    if expected_date == today.isoformat():
        return STATUS_EXPECTED_TODAY
    return STATUS_UNKNOWN


def extract_tracking_update(
    *,
    carrier: str,
    tracking_code: str,
    html: str,
    fetched_url: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Extract normalized tracking fields from a carrier tracking page."""
    today = today or date.today()
    text = _page_to_text(html)
    compact_text = re.sub(r"\s+", " ", text).strip()
    lowered = compact_text.lower()

    update: dict[str, Any] = {
        "carrier": carrier,
        "tracking_code": tracking_code,
        "tracking_url": fetched_url or build_tracking_url(carrier, tracking_code),
        "tracking_refresh_url": fetched_url or build_tracking_url(carrier, tracking_code),
        "tracking_refresh_source": "public_tracking_page",
        "tracking_refresh_supported": True,
    }

    if is_blocked_tracking_text(compact_text):
        update["tracking_refresh_error"] = TRACKING_BLOCKED_ERROR
        update["tracking_status_text"] = ""
        update["status"] = STATUS_UNKNOWN
        return update

    if _needs_human_or_postcode(compact_text):
        update["tracking_refresh_error"] = "tracking_page_needs_human_or_postcode"
        update["tracking_status_text"] = ""
        update["status"] = STATUS_UNKNOWN
        return update

    update["tracking_status_text"] = _status_excerpt(compact_text, carrier)

    expected_date = _extract_expected_date(compact_text, today)
    if expected_date:
        update["expected_date"] = expected_date

    start, end = _extract_time_window(compact_text)
    if start and end:
        update["delivery_window_start"] = start
        update["delivery_window_end"] = end

    pickup_location = _extract_pickup_location(compact_text)
    if pickup_location:
        update["pickup_location"] = pickup_location

    update["status"] = _extract_status(compact_text, expected_date, today)
    if normalize_carrier(carrier) == "gls":
        _apply_gls_tracking_text(update, compact_text, today)
    return update


def is_blocked_tracking_text(value: str | None) -> bool:
    """Return true when a carrier response is an error, auth or bot page."""
    lowered = clean_text(value).lower()
    return any(hint in lowered for hint in BLOCKED_HINTS)


def _needs_human_or_postcode(value: str | None) -> bool:
    lowered = clean_text(value).lower()
    return any(hint in lowered for hint in HUMAN_REQUIRED_HINTS)


def extract_tracking_update_from_json(
    *,
    carrier: str,
    tracking_code: str,
    payload: Any,
    fetched_url: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Extract normalized tracking fields from a public carrier JSON response."""
    today = today or date.today()
    shipment = _first_shipment(payload)
    flattened = _flatten_json(shipment if shipment is not None else payload)
    compact_text = re.sub(r"\s+", " ", flattened).strip()

    update: dict[str, Any] = {
        "carrier": carrier,
        "tracking_code": tracking_code,
        "tracking_url": build_tracking_url(carrier, tracking_code),
        "tracking_refresh_source": "public_tracking_api",
        "tracking_refresh_supported": True,
        "tracking_status_text": _status_excerpt(compact_text, carrier),
    }
    if fetched_url:
        update["tracking_api_url"] = fetched_url
        update["tracking_refresh_url"] = fetched_url

    delivered_at = _first_nested_value(
        shipment,
        ("deliveredAt", "delivered_at", "deliveryDate", "delivery_date"),
    )
    delivery_moment = _first_nested_value(
        shipment,
        (
            "momentIndication",
            "moment",
            "expectedDeliveryDate",
            "plannedDate",
            "planned_date",
            "deliveryDate",
            "expectedDeliveryMoment",
        ),
    )
    state_text = str(
        _first_nested_value(shipment, ("stateMessage", "message", "status", "statusMessage"))
        or ""
    )

    if delivered_at or _json_has_completed_phase(shipment, "DELIVERED"):
        update["status"] = STATUS_DELIVERED
        delivered_when = _datetime_from_value(delivered_at or delivery_moment)
        if delivered_when:
            update["tracking_status_text"] = f"Delivered at {delivered_when.strftime('%Y-%m-%d %H:%M')}"
        return update

    planned_at = _datetime_from_value(delivery_moment)
    window_start_at, _window_end_at = _datetime_window_from_json(shipment)
    if not planned_at and window_start_at:
        planned_at = window_start_at
    if planned_at:
        update["expected_date"] = planned_at.date().isoformat()

    start, end = _extract_window_from_json(shipment)
    if not start or not end:
        start, end = _extract_time_window(compact_text)
    if start and end:
        update["delivery_window_start"] = start
        update["delivery_window_end"] = end

    if carrier == "dhl":
        dhl_status_text = _dhl_status_text(shipment)
        if dhl_status_text:
            update["tracking_status_text"] = dhl_status_text
        events = _tracking_events_from_json(shipment)
        if events:
            update["extra"] = {"tracking_events": events}

        problem_phase = _first_completed_phase(shipment, ("PROBLEM", "EXCEPTION", "INTERVENTION"))
        if problem_phase:
            update["status"] = STATUS_UNKNOWN
            update["tracking_refresh_error"] = f"dhl_{problem_phase.lower()}"
            return update

    if _json_has_completed_phase(shipment, "IN_DELIVERY") or "OUT_FOR_DELIVERY" in compact_text:
        if planned_at and planned_at.date() != today:
            update["status"] = STATUS_IN_TRANSIT
        else:
            update["status"] = STATUS_EXPECTED_TODAY
            update.setdefault("expected_date", today.isoformat())
    elif carrier == "dhl" and _first_completed_phase(shipment, ("UNDERWAY", "DATA_RECEIVED")):
        update["status"] = STATUS_IN_TRANSIT
    else:
        update["status"] = _extract_status(f"{state_text} {compact_text}", update.get("expected_date"), today)
    return update


def build_fedex_tracking_payload(tracking_code: str) -> dict[str, Any]:
    """Build the public FedEx WTRK tracking payload."""
    return {
        "appDeviceType": "WTRK",
        "appType": "WTRK",
        "summaryView": False,
        "supportHTML": True,
        "supportCurrentLocation": True,
        "trackingInfo": [
            {
                "trackNumberInfo": {
                    "trackingCarrier": "",
                    "trackingNumber": tracking_code,
                    "trackingQualifier": "",
                }
            }
        ],
        "uniqueKey": "",
        "guestAuthenticationToken": "",
    }


def extract_fedex_tracking_update_from_json(
    *,
    tracking_code: str,
    payload: Any,
    fetched_url: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Extract normalized fields from FedEx WTRK JSON."""
    today = today or date.today()
    package = _first_fedex_package(payload)
    flattened = _flatten_json(package if package is not None else payload)
    compact_text = re.sub(r"\s+", " ", flattened).strip()

    update: dict[str, Any] = {
        "carrier": "fedex",
        "tracking_code": tracking_code,
        "tracking_url": build_tracking_url("fedex", tracking_code),
        "tracking_api_url": fetched_url,
        "tracking_refresh_url": fetched_url,
        "tracking_refresh_source": "fedex_public_api",
        "tracking_refresh_supported": True,
    }

    if not isinstance(package, dict):
        update["tracking_refresh_error"] = "fedex_api_no_package"
        update["tracking_status_text"] = ""
        update["status"] = STATUS_UNKNOWN
        return update

    if package.get("trackingNbr"):
        update["tracking_code"] = str(package["trackingNbr"])

    delivered = package.get("delivered") is True or str(package.get("keyStatusCD") or "").upper() == "DL"
    expected_date = _fedex_expected_date(package, today)
    if expected_date:
        update["expected_date"] = expected_date

    start, end = _fedex_time_window(package)
    if start and end:
        update["delivery_window_start"] = start
        update["delivery_window_end"] = end

    location = _fedex_location(package)
    status_text = _fedex_status_text(package, location)
    if status_text and not is_blocked_tracking_text(status_text):
        update["tracking_status_text"] = status_text

    if delivered:
        update["status"] = STATUS_DELIVERED
    elif package.get("deliveryToday") is True or expected_date == today.isoformat():
        update["status"] = STATUS_EXPECTED_TODAY
        update.setdefault("expected_date", today.isoformat())
    else:
        update["status"] = _extract_status(f"{compact_text} {status_text or ''}", expected_date, today)

    return update


def extract_fedex_tracking_update_from_mail(
    *,
    record: dict[str, Any],
    error: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Build a FedEx update from the latest mail when live tracking is blocked."""
    today = today or date.today()
    tracking_code = str(record.get("tracking_code") or "").strip()
    raw_excerpt = clean_text(str(record.get("raw_excerpt") or ""))
    expected_date = str(record.get("expected_date") or "") or _extract_expected_date(raw_excerpt, today)

    update: dict[str, Any] = {
        "carrier": "fedex",
        "tracking_code": tracking_code or None,
        "tracking_url": record.get("tracking_url") or build_tracking_url("fedex", tracking_code),
        "tracking_refresh_url": record.get("tracking_url") or build_tracking_url("fedex", tracking_code),
        "tracking_refresh_source": "fedex_mail_fallback",
        "tracking_refresh_supported": True,
    }

    if expected_date:
        update["expected_date"] = expected_date

    start = record.get("delivery_window_start")
    end = record.get("delivery_window_end")
    if start and end and start != end:
        update["delivery_window_start"] = start
        update["delivery_window_end"] = end

    status_text = _fedex_mail_status_text(raw_excerpt, expected_date)
    if status_text:
        update["tracking_status_text"] = status_text

    status = str(record.get("status") or "")
    inferred_status = _extract_status(raw_excerpt, expected_date, today)
    if inferred_status in {STATUS_DELIVERED, STATUS_READY_FOR_PICKUP}:
        update["status"] = inferred_status
    elif expected_date == today.isoformat() and status not in {STATUS_DELIVERED, STATUS_READY_FOR_PICKUP}:
        update["status"] = STATUS_EXPECTED_TODAY
    elif status and status != STATUS_UNKNOWN:
        update["status"] = status
    else:
        update["status"] = inferred_status

    if not (expected_date or status_text or update["status"] != STATUS_UNKNOWN):
        update["tracking_refresh_error"] = error or TRACKING_BLOCKED_ERROR
    return update


def _page_to_text(html: str) -> str:
    parts: list[str] = []
    parts.extend(_json_script_text(html))
    parts.append(html)
    return clean_text("\n".join(parts))


def _json_script_text(html: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(
        r"(?is)<script[^>]+(?:application/(?:ld\+)?json|__NEXT_DATA__)[^>]*>(.*?)</script>",
        html,
    ):
        raw = clean_text(match.group(1))
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            values.append(raw)
            continue
        values.append(_flatten_json(parsed))
    return values


def _flatten_json(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_json(child) for child in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_json(child) for child in value)
    if value is None:
        return ""
    return str(value)


def _first_shipment(payload: Any) -> Any:
    if isinstance(payload, list):
        return payload[0] if payload else None
    return payload


def _first_fedex_package(payload: Any) -> Any:
    packages = _first_nested_value(payload, ("packages",))
    if isinstance(packages, list) and packages:
        return packages[0]
    if isinstance(payload, dict) and _looks_like_fedex_package(payload):
        return payload
    return None


def _looks_like_fedex_package(value: Any) -> bool:
    return isinstance(value, dict) and any(key in value for key in ("trackingNbr", "keyStatus", "mainStatus"))


def _first_nested_value(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key]:
                return value[key]
        for child in value.values():
            found = _first_nested_value(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_nested_value(item, keys)
            if found:
                return found
    return None


def _dhl_status_text(payload: Any) -> str | None:
    event = _latest_event(payload)
    if not isinstance(event, dict):
        return None
    parts = [
        clean_text(str(event.get(key) or ""))
        for key in ("status", "category", "description", "remark")
        if event.get(key)
    ]
    facility = event.get("facility")
    if isinstance(facility, dict):
        parts.extend(
            clean_text(str(facility.get(key) or ""))
            for key in ("city", "countryCode")
            if facility.get(key)
        )
    location = event.get("location")
    if isinstance(location, dict):
        parts.extend(
            clean_text(str(location.get(key) or ""))
            for key in ("city", "countryCode")
            if location.get(key)
        )
    seen: set[str] = set()
    unique = [part for part in parts if part and not (part in seen or seen.add(part))]
    return " - ".join(unique)[:220] if unique else None


def _latest_event(value: Any) -> Any:
    events = _first_nested_value(value, ("events", "scanEventList", "eventHistory"))
    if isinstance(events, list) and events:
        return events[-1]
    return None


def _tracking_events_from_json(value: Any) -> list[dict[str, str]]:
    source = _first_nested_value(value, ("events", "scanEventList", "eventHistory"))
    if not isinstance(source, list):
        return []
    events: list[dict[str, str]] = []
    for item in source[-20:]:
        if not isinstance(item, dict):
            continue
        event = {
            "timestamp": clean_text(str(_first_nested_value(item, ("time", "timestamp", "date", "dateTime")) or "")),
            "status": clean_text(str(_first_nested_value(item, ("status", "category", "description", "remark")) or "")),
            "location": _json_location_text(item),
        }
        compact = {key: value for key, value in event.items() if value}
        if compact:
            events.append(compact)
    return events


def _json_location_text(value: Any) -> str:
    location = _first_nested_value(value, ("facility", "location", "scanLocation", "scanLocationAddress"))
    if isinstance(location, dict):
        parts = [
            clean_text(str(location.get(key) or ""))
            for key in ("city", "stateOrProvinceCode", "countryCode")
            if location.get(key)
        ]
        return ", ".join(parts)
    return clean_text(str(location or ""))


def _first_completed_phase(value: Any, phases: tuple[str, ...]) -> str | None:
    for phase in phases:
        if _json_has_completed_phase(value, phase):
            return phase
    return None


def _json_has_completed_phase(value: Any, phase: str) -> bool:
    phase = _phase_key(phase)
    if isinstance(value, dict):
        current_phase = _phase_key(value.get("phase"))
        event_key = _phase_key(value.get("key"))
        status = _phase_key(value.get("status"))
        category = _phase_key(value.get("category"))
        completed = value.get("completed")
        if current_phase == phase and (completed is not False):
            return True
        if event_key == phase or status == phase or category == phase:
            return True
        return any(_json_has_completed_phase(child, phase) for child in value.values())
    if isinstance(value, list):
        return any(_json_has_completed_phase(item, phase) for item in value)
    return False


def _phase_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").upper()).strip("_")


def _extract_window_from_json(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        for interval_key in ("plannedDeliveryTimeframe", "expectedDeliveryTimeframe", "deliveryTimeframe"):
            start_at, end_at = _datetime_window_from_value(value.get(interval_key))
            if start_at and end_at:
                return (start_at.strftime("%H:%M"), end_at.strftime("%H:%M"))

        candidates = (
            ("deliveryWindowStart", "deliveryWindowEnd"),
            ("delivery_window_start", "delivery_window_end"),
            ("expectedFrom", "expectedTo"),
            ("plannedFrom", "plannedTo"),
            ("planned_from", "planned_to"),
            ("from", "to"),
            ("start", "end"),
        )
        for start_key, end_key in candidates:
            if value.get(start_key) and value.get(end_key):
                start = _time_from_any(value[start_key])
                end = _time_from_any(value[end_key])
                if start and end:
                    return (start, end)
        for child in value.values():
            start, end = _extract_window_from_json(child)
            if start and end:
                return (start, end)
    elif isinstance(value, list):
        for item in value:
            start, end = _extract_window_from_json(item)
            if start and end:
                return (start, end)
    return (None, None)


def _datetime_window_from_json(value: Any) -> tuple[datetime | None, datetime | None]:
    if isinstance(value, dict):
        for interval_key in ("plannedDeliveryTimeframe", "expectedDeliveryTimeframe", "deliveryTimeframe"):
            start_at, end_at = _datetime_window_from_value(value.get(interval_key))
            if start_at and end_at:
                return (start_at, end_at)
        for child in value.values():
            start_at, end_at = _datetime_window_from_json(child)
            if start_at and end_at:
                return (start_at, end_at)
    elif isinstance(value, list):
        for item in value:
            start_at, end_at = _datetime_window_from_json(item)
            if start_at and end_at:
                return (start_at, end_at)
    return (None, None)


def _datetime_window_from_value(value: Any) -> tuple[datetime | None, datetime | None]:
    if not value:
        return (None, None)
    parts = str(value).split("/", 1)
    if len(parts) != 2:
        return (None, None)
    return (_datetime_from_value(parts[0]), _datetime_from_value(parts[1]))


def _datetime_from_value(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _apply_gls_tracking_text(update: dict[str, Any], compact_text: str, today: date) -> None:
    lowered = compact_text.lower()
    if _has_delivered_signal(lowered):
        update["status"] = STATUS_DELIVERED
        return
    if any(
        term in lowered
        for term in (
            "loaded onto the delivery vehicle",
            "out for delivery",
            "in aflevering",
            "onderweg naar het afleveradres",
            "wordt vandaag bezorgd",
            "vandaag bezorgd",
        )
    ):
        update["status"] = STATUS_EXPECTED_TODAY
        update.setdefault("expected_date", today.isoformat())
        return
    if any(
        term in lowered
        for term in (
            "parcel center",
            "pakketcentrum",
            "gls depot",
            "depot",
            "aangekondigd bij gls",
            "pakket is ontvangen",
            "onderweg",
            "in transit",
        )
    ):
        update["status"] = STATUS_IN_TRANSIT
        return
    if any(term in lowered for term in ("niet geleverd", "delivery attempt", "bezorgpoging", "exception")):
        update["status"] = STATUS_UNKNOWN
        update["tracking_refresh_error"] = "gls_delivery_problem"


def _fedex_expected_date(package: dict[str, Any], today: date) -> str | None:
    for key in ("estDeliveryDt", "actDeliveryDt", "displayEstDeliveryDt", "displayActDeliveryDt"):
        value = package.get(key)
        parsed = _datetime_from_value(value)
        if parsed:
            return parsed.date().isoformat()
        extracted = _extract_expected_date(str(value or ""), today)
        if extracted:
            return extracted
    return None


def _fedex_time_window(package: dict[str, Any]) -> tuple[str | None, str | None]:
    window = package.get("estDelTimeWindow")
    if isinstance(window, dict):
        for start_key, end_key in (
            ("displayEstDelTmWindowTmStart", "displayEstDelTmWindowTmEnd"),
            ("estDelTmWindowTmStart", "estDelTmWindowTmEnd"),
            ("startTime", "endTime"),
        ):
            start = _time_from_any(window.get(start_key))
            end = _time_from_any(window.get(end_key))
            if start and end:
                return (start, end)
    return _extract_time_window(
        " ".join(
            str(package.get(key) or "")
            for key in ("displayEstDeliveryTm", "displayEstDeliveryDateTime", "statusWithDetails")
        )
    )


def _fedex_location(package: dict[str, Any]) -> str | None:
    for source in (
        package.get("statusLocationAddress"),
        _first_nested_value(package.get("scanEventList"), ("scanLocation",)),
    ):
        if not isinstance(source, dict):
            continue
        parts = [
            clean_text(str(source.get(key) or ""))
            for key in ("city", "stateOrProvinceCode", "countryCode")
            if source.get(key)
        ]
        if parts:
            return ", ".join(parts)
    return None


def _fedex_status_text(package: dict[str, Any], location: str | None) -> str | None:
    parts = [
        clean_text(str(package.get(key) or ""))
        for key in ("mainStatus", "keyStatus", "statusWithDetails", "subStatus")
        if package.get(key)
    ]
    if location:
        parts.append(location)
    seen: set[str] = set()
    unique = [part for part in parts if part and not (part in seen or seen.add(part))]
    return " - ".join(unique)[:220] if unique else None


def _fedex_mail_status_text(raw_excerpt: str, expected_date: str | None) -> str | None:
    if not raw_excerpt:
        return None
    parts: list[str] = []
    sender = re.search(
        r"(?i)uw zending van\s+(.+?)\s+is\s+(?:onderweg|afgeleverd|bezorgd|geleverd)",
        raw_excerpt,
    )
    if sender:
        parts.append(clean_text(sender.group(1)))
    if expected_date:
        parts.append(f"gepland {expected_date}")
    service = re.search(r"(?i)\bservice\s+(.+?)(?:\s+tracking|\s+aantal|\s+totaal|$)", raw_excerpt)
    if service:
        parts.append(clean_text(service.group(1)))
    seen: set[str] = set()
    unique = [part for part in parts if part and not (part in seen or seen.add(part))]
    return "FedEx mail: " + " - ".join(unique)[:200] if unique else None


def _time_from_any(value: Any) -> str | None:
    parsed = _datetime_from_value(value)
    if parsed:
        return parsed.strftime("%H:%M")
    match = re.search(r"\b([0-2]?\d)[:.h]([0-5]\d)\b", str(value))
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def _extract_status(value: str, expected_date: str | None, today: date) -> str:
    lowered = value.lower()
    if any(term in lowered for term in ("ready for pickup", "ligt klaar", "af te halen", "pickup point", "parcelshop")):
        return STATUS_READY_FOR_PICKUP
    if _has_delivered_signal(lowered):
        return STATUS_DELIVERED
    if any(
        term in lowered
        for term in (
            "out for delivery",
            "in delivery",
            "wordt vandaag bezorgd",
            "vandaag verwacht",
            "expected today",
            "will be delivered",
        )
    ):
        return STATUS_EXPECTED_TODAY
    if expected_date == today.isoformat():
        return STATUS_EXPECTED_TODAY
    if any(
        term in lowered
        for term in (
            "in transit",
            "underway",
            "onderweg",
            "sortering",
            "sorting",
            "shipment",
            "zending",
            "data received",
            "parcel sorted",
        )
    ):
        return STATUS_IN_TRANSIT
    return STATUS_UNKNOWN


def _has_delivered_signal(lowered: str) -> bool:
    if any(
        future in lowered
        for future in (
            "will be delivered",
            "expected to be delivered",
            "wordt bezorgd",
            "wordt vandaag bezorgd",
            "wordt morgen bezorgd",
            "wordt afgeleverd",
            "wordt vandaag afgeleverd",
            "wordt morgen afgeleverd",
            "zal worden bezorgd",
        )
    ):
        return False
    return any(
        term in lowered
        for term in (
            "afgeleverd:",
            "is afgeleverd",
            "afgeleverd om",
            "is bezorgd",
            "bezorgd om",
            "has been delivered",
            "was delivered",
            "delivered at",
            "delivered on",
            "delivered",
            "bezorgd",
            "afgeleverd",
            "successfully delivered",
        )
    )


def _extract_expected_date(value: str, today: date) -> str | None:
    lowered = value.lower()
    if re.search(r"\b(vandaag|today)\b", lowered):
        return today.isoformat()
    if re.search(r"\b(morgen|tomorrow)\b", lowered):
        return (today + timedelta(days=1)).isoformat()

    for pattern in (
        r"\b(20\d{2})-(\d{2})-(\d{2})(?:[tT ][0-2]\d:[0-5]\d(?::[0-5]\d)?)?",
        r"\b(\d{1,2})[-/](\d{1,2})(?:[-/](20\d{2}|\d{2}))?\b",
    ):
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            if pattern.startswith("\\b(20"):
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
            year = int(match.group(3) or today.year)
            if year < 100:
                year += 2000
            return date(year, int(match.group(2)), int(match.group(1))).isoformat()
        except ValueError:
            continue
    return None


def _extract_time_window(value: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"(?i)(?:tussen|between|van|from)?\s*([0-2]?\d)[:.h]([0-5]\d)\s*(?:en|and|-|tot|to|until)\s*([0-2]?\d)[:.h]([0-5]\d)",
        value,
    )
    if match:
        return (
            f"{int(match.group(1)):02d}:{match.group(2)}",
            f"{int(match.group(3)):02d}:{match.group(4)}",
        )

    iso_times = []
    for match in re.finditer(r"\b20\d{2}-\d{2}-\d{2}[tT ]([0-2]\d):([0-5]\d)", value):
        iso_times.append(f"{match.group(1)}:{match.group(2)}")
    if len(iso_times) >= 2:
        return (iso_times[0], iso_times[1])
    return (None, None)


def _extract_pickup_location(value: str) -> str | None:
    match = re.search(
        r"(?i)\b(?:bij|at|pickup point|parcelshop|servicepoint)\s+(?:de\s+)?([A-Z][A-Za-z0-9 &'.-]{2,70})(?:[,.]|$)",
        value,
    )
    if match:
        return match.group(1).strip(" .,-")[:70]
    return None


def _status_excerpt(value: str, carrier: str) -> str | None:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", value)
    carrier_lower = carrier.lower()
    keywords = (
        carrier_lower,
        "bezorgd",
        "delivered",
        "onderweg",
        "in transit",
        "verwacht",
        "expected",
        "vandaag",
        "today",
        "pickup",
        "afhalen",
    )
    for sentence in sentences:
        cleaned = sentence.strip()
        if 12 <= len(cleaned) <= 220 and any(keyword in cleaned.lower() for keyword in keywords):
            return cleaned[:220]
    return value[:220] if value else None
