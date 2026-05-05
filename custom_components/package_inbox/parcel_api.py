"""Parcel app compatibility helpers.

The integration is mail/local-first today. These mappings are kept separate so
an optional Parcel REST API source can be added without changing dashboard code.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    STATUS_DELIVERED,
    STATUS_EXPECTED_TODAY,
    STATUS_IN_TRANSIT,
    STATUS_READY_FOR_PICKUP,
    STATUS_UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class ParcelApiStatus:
    """A Parcel API status-code mapping."""

    code: int
    parcel_label: str
    status: str
    active: bool


PARCEL_API_STATUS_CODES: dict[int, ParcelApiStatus] = {
    0: ParcelApiStatus(0, "completed", STATUS_DELIVERED, False),
    1: ParcelApiStatus(1, "frozen", STATUS_UNKNOWN, True),
    2: ParcelApiStatus(2, "in_transit", STATUS_IN_TRANSIT, True),
    3: ParcelApiStatus(3, "pickup", STATUS_READY_FOR_PICKUP, True),
    4: ParcelApiStatus(4, "out_for_delivery", STATUS_EXPECTED_TODAY, True),
    5: ParcelApiStatus(5, "not_found", STATUS_UNKNOWN, True),
    6: ParcelApiStatus(6, "failed", STATUS_UNKNOWN, True),
    7: ParcelApiStatus(7, "exception", STATUS_UNKNOWN, True),
    8: ParcelApiStatus(8, "info_received", STATUS_IN_TRANSIT, True),
}


def parcel_api_status_from_code(code: int | str | None) -> ParcelApiStatus | None:
    """Return the Parcel API mapping for a status code."""
    try:
        normalized = int(str(code).strip())
    except (TypeError, ValueError):
        return None
    return PARCEL_API_STATUS_CODES.get(normalized)


def map_parcel_api_status_code(code: int | str | None) -> str:
    """Map a Parcel API status code to the internal Home Assistant status."""
    status = parcel_api_status_from_code(code)
    return status.status if status else STATUS_UNKNOWN
