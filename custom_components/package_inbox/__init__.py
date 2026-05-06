"""Parcels integration."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, time, timedelta
from email import message_from_bytes, message_from_string, policy
import base64
import hashlib
import html as html_lib
import json
import logging
import mimetypes
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from aiohttp import ClientError, ClientTimeout
import voluptuous as vol

from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .carrier_rules import normalize_carrier
from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_DELIVERY_HOUSE_NUMBER,
    CONF_DELIVERY_POSTCODE,
    CONF_ENABLE_AI_CLASSIFICATION,
    CONF_ENABLE_AI_FALLBACK,
    CONF_ENABLE_EVENT_LISTENER,
    CONF_ENABLE_TRACKING_REFRESH,
    CONF_IMAP_ENTRY_ID,
    CONF_MATRIX_ROOM_ID,
    CONF_NOTIFY_SCRIPT,
    CONF_POSTNL_DELIVERY_SENSOR,
    CONF_PUBLIC_QR_DIR,
    CONF_TRACKING_REFRESH_MINUTES,
    CONF_TRACKING_SCRAPER_TOKEN,
    CONF_TRACKING_SCRAPER_URL,
    CONF_TRACKING_TIMEOUT,
    CONF_TRACKING_USER_AGENT,
    DEFAULT_AI_TASK_ENTITY,
    DEFAULT_DELIVERY_HOUSE_NUMBER,
    DEFAULT_DELIVERY_POSTCODE,
    DEFAULT_MATRIX_ROOM_ID,
    DEFAULT_NOTIFY_SCRIPT,
    DEFAULT_POSTNL_DELIVERY_SENSOR,
    DEFAULT_PUBLIC_QR_DIR,
    DEFAULT_TRACKING_REFRESH_MINUTES,
    DEFAULT_TRACKING_SCRAPER_TOKEN,
    DEFAULT_TRACKING_SCRAPER_URL,
    DEFAULT_TRACKING_TIMEOUT,
    DEFAULT_TRACKING_USER_AGENT,
    DOMAIN,
    SERVICE_ADD_PACKAGE,
    SERVICE_DEBUG_PARSE,
    SERVICE_DELETE_PACKAGE,
    SERVICE_MARK_PICKED_UP,
    SERVICE_PROCESS_IMAP_EVENT,
    SERVICE_REFRESH_TRACKING,
    SERVICE_SEND_MORNING_SUMMARY,
    SERVICE_SEND_PICKUP_SUMMARY,
    SERVICE_SET_STATUS,
    STATUS_DELIVERED,
    STATUS_EXPECTED_TODAY,
    STATUS_IN_TRANSIT,
    STATUS_PICKED_UP,
    STATUS_READY_FOR_PICKUP,
    STATUS_UNKNOWN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .dashboard import build_dashboard_snapshot
from .parser import clean_text, is_likely_package_email, parse_email, stable_key
from .record_merge import apply_vinted_cross_reference, merge_tracking_update, reconcile_vinted_carrier_links
from .tracking import (
    TRACKING_BLOCKED_ERROR,
    build_fedex_tracking_api_url,
    build_fedex_tracking_payload,
    build_tracking_api_url,
    build_tracking_url,
    extract_fedex_tracking_update_from_mail,
    extract_fedex_tracking_update_from_json,
    extract_tracking_update,
    extract_tracking_update_from_json,
    is_blocked_tracking_text,
    normalize_tracking_scraper_update,
    supports_public_tracking,
)
from .window import DEFAULT_WINDOW_MARGIN_MINUTES, build_delivery_snapshot, dedupe_delivery_records

_LOGGER = logging.getLogger(__name__)

AI_CARRIER_LIST = (
    "postnl|dhl|dpd|gls|fedex|chronopost|ups|trunkrs|homerr|cycloon|instabox|"
    "transmission|dachser|dynalogic|gofo|dragonfly|amazon|vinted|apotheek|unknown"
)
TRACKING_SCRAPER_CARRIERS = {"fedex", "chronopost"}

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_IMAP_ENTRY_ID): cv.string,
                vol.Optional(CONF_NOTIFY_SCRIPT, default=DEFAULT_NOTIFY_SCRIPT): cv.string,
                vol.Optional(CONF_MATRIX_ROOM_ID, default=DEFAULT_MATRIX_ROOM_ID): cv.string,
                vol.Optional(CONF_AI_TASK_ENTITY, default=DEFAULT_AI_TASK_ENTITY): cv.string,
                vol.Optional(CONF_DELIVERY_POSTCODE, default=DEFAULT_DELIVERY_POSTCODE): cv.string,
                vol.Optional(CONF_DELIVERY_HOUSE_NUMBER, default=DEFAULT_DELIVERY_HOUSE_NUMBER): cv.string,
                vol.Optional(CONF_ENABLE_AI_CLASSIFICATION, default=True): cv.boolean,
                vol.Optional(CONF_ENABLE_AI_FALLBACK, default=True): cv.boolean,
                vol.Optional(CONF_POSTNL_DELIVERY_SENSOR, default=DEFAULT_POSTNL_DELIVERY_SENSOR): cv.string,
                vol.Optional(CONF_PUBLIC_QR_DIR, default=DEFAULT_PUBLIC_QR_DIR): cv.string,
                vol.Optional(CONF_ENABLE_EVENT_LISTENER, default=False): cv.boolean,
                vol.Optional(CONF_ENABLE_TRACKING_REFRESH, default=True): cv.boolean,
                vol.Optional(
                    CONF_TRACKING_REFRESH_MINUTES,
                    default=DEFAULT_TRACKING_REFRESH_MINUTES,
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=1440)),
                vol.Optional(CONF_TRACKING_TIMEOUT, default=DEFAULT_TRACKING_TIMEOUT): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=5, max=60),
                ),
                vol.Optional(CONF_TRACKING_USER_AGENT, default=DEFAULT_TRACKING_USER_AGENT): cv.string,
                vol.Optional(CONF_TRACKING_SCRAPER_URL, default=DEFAULT_TRACKING_SCRAPER_URL): cv.string,
                vol.Optional(CONF_TRACKING_SCRAPER_TOKEN, default=DEFAULT_TRACKING_SCRAPER_TOKEN): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

PROCESS_IMAP_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required("uid"): cv.string,
        vol.Optional("subject"): cv.string,
        vol.Optional("sender"): cv.string,
        vol.Optional("text"): cv.string,
        vol.Optional("headers"): vol.Any(dict, list, cv.string),
        vol.Optional("notify", default=True): cv.boolean,
        vol.Optional("mark_seen", default=True): cv.boolean,
    }
)

SUMMARY_SCHEMA = vol.Schema({vol.Optional("notify", default=True): cv.boolean})
PICKUP_SUMMARY_SCHEMA = vol.Schema({vol.Optional("notify", default=True): cv.boolean})
MARK_PICKED_UP_SCHEMA = vol.Schema({vol.Required("package_key"): cv.string})
DELETE_PACKAGE_SCHEMA = vol.Schema({vol.Required("package_key"): cv.string})
SET_STATUS_SCHEMA = vol.Schema(
    {
        vol.Required("package_key"): cv.string,
        vol.Required("status"): vol.In(
            [
                STATUS_DELIVERED,
                STATUS_EXPECTED_TODAY,
                STATUS_IN_TRANSIT,
                STATUS_PICKED_UP,
                STATUS_READY_FOR_PICKUP,
                STATUS_UNKNOWN,
                "cancelled",
            ]
        ),
        vol.Optional("notify", default=False): cv.boolean,
    }
)
REFRESH_TRACKING_SCHEMA = vol.Schema(
    {
        vol.Optional("notify", default=True): cv.boolean,
        vol.Optional("force", default=False): cv.boolean,
        vol.Optional("package_key"): cv.string,
    }
)
DEBUG_PARSE_SCHEMA = vol.Schema(
    {
        vol.Optional("subject"): cv.string,
        vol.Optional("sender"): cv.string,
        vol.Required("text"): cv.string,
    }
)
ADD_PACKAGE_SCHEMA = vol.Schema(
    {
        vol.Required("package"): dict,
        vol.Optional("notify", default=True): cv.boolean,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Parcels from YAML."""
    conf = config.get(DOMAIN)
    if conf is None:
        return True

    manager = PackageInboxManager(hass, dict(conf))
    await manager.async_load()
    hass.data[DOMAIN] = manager
    await manager.async_setup()
    await discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    await discovery.async_load_platform(hass, "binary_sensor", DOMAIN, {}, config)
    return True


class PackageInboxManager:
    """Manage package records, IMAP processing and notifications."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config
        self.store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = {}

    async def async_load(self) -> None:
        """Load stored package state."""
        loaded = await self.store.async_load()
        self.data = loaded if isinstance(loaded, dict) else {}
        self.data.setdefault("packages", {})
        self.data.setdefault("processed_messages", {})
        self.data.setdefault("notifications", {})
        self.data["notifications"].setdefault("morning_summary", {})
        self.data["notifications"].setdefault("pickup_summary", {})
        self.data["notifications"].setdefault("pickup_notified", [])
        self.data["notifications"].setdefault("extra_today_notified", [])

    async def async_setup(self) -> None:
        """Register services and optional event listener."""
        self._register_services()

        if self.config.get(CONF_ENABLE_EVENT_LISTENER):
            self.hass.bus.async_listen("imap_content", self._async_handle_imap_event)

    def _register_services(self) -> None:
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_PROCESS_IMAP_EVENT,
            self._service_process_imap_event,
            schema=PROCESS_IMAP_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_MORNING_SUMMARY,
            self._service_send_morning_summary,
            schema=SUMMARY_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_TRACKING,
            self._service_refresh_tracking,
            schema=REFRESH_TRACKING_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_PICKUP_SUMMARY,
            self._service_send_pickup_summary,
            schema=PICKUP_SUMMARY_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_MARK_PICKED_UP,
            self._service_mark_picked_up,
            schema=MARK_PICKED_UP_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_DELETE_PACKAGE,
            self._service_delete_package,
            schema=DELETE_PACKAGE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_SET_STATUS,
            self._service_set_status,
            schema=SET_STATUS_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_DEBUG_PARSE,
            self._service_debug_parse,
            schema=DEBUG_PARSE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_PACKAGE,
            self._service_add_package,
            schema=ADD_PACKAGE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    async def _async_handle_imap_event(self, event: Event) -> None:
        await self.async_process_imap_event(dict(event.data), notify=True, mark_seen=True)

    async def _service_process_imap_event(self, call: ServiceCall) -> ServiceResponse:
        return await self.async_process_imap_event(
            dict(call.data),
            notify=bool(call.data.get("notify", True)),
            mark_seen=bool(call.data.get("mark_seen", True)),
        )

    async def _service_send_morning_summary(self, call: ServiceCall) -> ServiceResponse:
        return await self.async_send_morning_summary(notify=bool(call.data.get("notify", True)))

    async def _service_send_pickup_summary(self, call: ServiceCall) -> ServiceResponse:
        return await self.async_send_pickup_summary(notify=bool(call.data.get("notify", True)))

    async def _service_mark_picked_up(self, call: ServiceCall) -> ServiceResponse:
        return await self.async_mark_picked_up(package_key=str(call.data["package_key"]))

    async def _service_delete_package(self, call: ServiceCall) -> ServiceResponse:
        return await self.async_delete_package(package_key=str(call.data["package_key"]))

    async def _service_set_status(self, call: ServiceCall) -> ServiceResponse:
        return await self.async_set_status(
            package_key=str(call.data["package_key"]),
            status=str(call.data["status"]),
            notify=bool(call.data.get("notify", False)),
        )

    async def _service_refresh_tracking(self, call: ServiceCall) -> ServiceResponse:
        return await self.async_refresh_tracking(
            notify=bool(call.data.get("notify", True)),
            force=bool(call.data.get("force", False)),
            package_key=call.data.get("package_key"),
        )

    async def _service_debug_parse(self, call: ServiceCall) -> ServiceResponse:
        records = parse_email(
            subject=call.data.get("subject"),
            sender=call.data.get("sender"),
            text=call.data["text"],
            today=dt_util.now().date(),
        )
        return {"matched": bool(records), "records": records}

    async def _service_add_package(self, call: ServiceCall) -> ServiceResponse:
        records = [dict(call.data["package"])]
        stored = await self._async_store_records(records, notify=bool(call.data.get("notify", True)))
        return {"stored": stored}

    async def async_refresh_tracking(
        self,
        *,
        notify: bool,
        force: bool = False,
        package_key: str | None = None,
    ) -> dict[str, Any]:
        """Refresh stored packages with public track-and-trace information."""
        if not self.config.get(CONF_ENABLE_TRACKING_REFRESH, True) and not force:
            return {"refreshed": [], "skipped": [{"reason": "tracking_refresh_disabled"}]}

        packages: dict[str, dict[str, Any]] = self.data["packages"]
        if package_key:
            if package_key not in packages:
                return {"refreshed": [], "skipped": [{"key": package_key, "reason": "unknown_package_key"}]}
            candidates = [(package_key, packages[package_key])]
        else:
            candidates = list(packages.items())

        refreshed: list[str] = []
        skipped: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        diagnostics: list[dict[str, Any]] = []

        vinted_records, vinted_diagnostics = await self._async_vinted_sidecar_records_for_refresh(
            package_key=package_key,
            candidates=candidates,
        )
        diagnostics.extend(vinted_diagnostics)
        if vinted_records:
            stored = await self._async_store_records(vinted_records, notify=notify)
            refreshed.extend(stored)

        for key, current in candidates:
            record = _normalize_record(current)
            record["key"] = key

            reason = self._tracking_skip_reason(record, force=force)
            if reason:
                skipped.append({"key": key, "reason": reason})
                continue

            try:
                update = await self._async_tracking_update_for_record(record)
            except Exception as err:  # pragma: no cover - defensive boundary around network/parser code
                _LOGGER.warning("Tracking refresh failed for %s: %s", key, err)
                errors.append({"key": key, "error": str(err)})
                continue

            diagnostics.append(_tracking_diagnostic(key, record, update))
            merged = merge_tracking_update(current, update, dt_util.now().isoformat())
            stored = await self._async_store_records([merged], notify=notify)
            refreshed.extend(stored)

        return {
            "refreshed": refreshed,
            "skipped": skipped,
            "errors": errors,
            "diagnostics": diagnostics,
            "count": len(refreshed),
        }

    async def _async_vinted_sidecar_records_for_refresh(
        self,
        *,
        package_key: str | None,
        candidates: list[tuple[str, dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if package_key and not any(_record_has_vinted_source(record) for _, record in candidates):
            return ([], [])
        result = await self._async_fetch_vinted_sidecar_records()
        if not result:
            return ([], [])
        records = result.get("records") if isinstance(result.get("records"), list) else []
        diagnostics = [
            {
                "key": "vinted_sidecar",
                "carrier": "vinted",
                "status": result.get("status"),
                "source": "vinted_sidecar",
                "supported": result.get("supported", True),
                "error": result.get("error"),
                "tracking_refresh_url": result.get("tracking_refresh_url"),
                "has_delivery_detail": bool(records),
                "count": len(records),
            }
        ]
        return ([record for record in records if isinstance(record, dict)], diagnostics)

    async def async_process_imap_event(
        self,
        event_data: dict[str, Any],
        *,
        notify: bool,
        mark_seen: bool,
    ) -> dict[str, Any]:
        """Process one imap_content event."""
        configured_entry = self.config[CONF_IMAP_ENTRY_ID]
        entry_id = str(event_data.get("entry_id") or event_data.get("entry") or "")
        uid = str(event_data.get("uid") or "")

        if entry_id != configured_entry:
            return {"processed": False, "reason": "wrong_entry", "entry_id": entry_id}
        if not uid:
            return {"processed": False, "reason": "missing_uid"}

        seed = _message_fields(event_data)
        seed_text = "\n".join(str(seed.get(k) or "") for k in ("subject", "sender", "text"))
        if not is_likely_package_email(seed.get("subject"), seed.get("sender"), seed_text):
            return {"processed": False, "reason": "not_package_like", "uid": uid}

        fetched: dict[str, Any] = {}
        try:
            fetched_response = await self.hass.services.async_call(
                "imap",
                "fetch",
                {"entry": entry_id, "uid": uid},
                blocking=True,
                return_response=True,
            )
            if isinstance(fetched_response, dict):
                fetched = fetched_response
        except Exception as err:
            _LOGGER.warning("Could not fetch IMAP uid %s: %s", uid, err)

        fields = _message_fields(event_data, fetched)
        text = fields.get("text") or seed_text

        classification: dict[str, Any] | None = None
        if self.config.get(CONF_ENABLE_AI_CLASSIFICATION, True):
            classification = await self._async_ai_classify_email(fields)

        local_reject_reason = _email_exclusion_reason(fields)
        if local_reject_reason or classification and classification.get("is_package") is False:
            rejection = classification or {}
            if local_reject_reason:
                rejection = {
                    **rejection,
                    "is_package": False,
                    "category": rejection.get("category") or "local_non_package",
                    "reason": local_reject_reason,
                    "confidence": rejection.get("confidence") or "high",
                }
            await self._async_store_rejection(fields, rejection)
            if mark_seen:
                await self._async_mark_seen(entry_id, uid)
            return {
                "processed": False,
                "reason": "not_a_package",
                "uid": uid,
                "classification": rejection,
            }

        records = parse_email(
            subject=fields.get("subject"),
            sender=fields.get("sender"),
            text=text,
            message_id=fields.get("message_id"),
            imap_uid=f"{entry_id}:{uid}",
            today=dt_util.now().date(),
        )

        if self.config.get(CONF_ENABLE_AI_FALLBACK) and _needs_ai_fallback(records, fields):
            ai_records = await self._async_ai_parse(fields)
            if ai_records:
                records = ai_records

        accepted_records = [record for record in records if not _record_exclusion_reason(record)]
        if records and not accepted_records:
            reason = "; ".join(_record_exclusion_reason(record) or "filtered" for record in records)
            rejection = {
                "is_package": False,
                "category": "record_filter",
                "reason": reason,
                "confidence": "high",
            }
            await self._async_store_rejection(fields, rejection)
            if mark_seen:
                await self._async_mark_seen(entry_id, uid)
            return {
                "processed": False,
                "reason": "record_filtered",
                "uid": uid,
                "classification": rejection,
            }
        records = accepted_records

        qr_files = await self._async_extract_qr_files(fetched, text, uid)
        if qr_files:
            for record in records:
                if record.get("status") == STATUS_READY_FOR_PICKUP and not record.get("qr_file_path"):
                    record["qr_file_path"] = qr_files[0]

        if not records:
            return {"processed": False, "reason": "parse_no_records", "uid": uid}

        records = await self._async_preflight_tracking(records)
        stored = await self._async_store_records(records, notify=notify)

        if mark_seen and stored:
            await self._async_mark_seen(entry_id, uid)

        return {
            "processed": bool(stored),
            "uid": uid,
            "stored": stored,
            "records": records,
        }

    async def _async_preflight_tracking(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Try live tracking before storing, so immediate notifications use checked state."""
        enriched: list[dict[str, Any]] = []
        for record in records:
            normalized = _normalize_record(record)
            carrier = normalized.get("carrier")
            if (
                not normalized.get("tracking_code")
                or normalized.get("status") in (STATUS_DELIVERED, STATUS_PICKED_UP, "cancelled")
                or carrier in ("amazon", "vinted", "apotheek", "unknown")
            ):
                enriched.append(record)
                continue
            try:
                update = await self._async_tracking_update_for_record(normalized)
            except Exception as err:  # pragma: no cover - preflight should never block mail intake
                _LOGGER.debug("Tracking preflight failed for %s: %s", normalized.get("tracking_code"), err)
                enriched.append(record)
                continue
            enriched.append(merge_tracking_update(record, update, dt_util.now().isoformat()))
        return enriched

    async def async_send_morning_summary(self, *, notify: bool) -> dict[str, Any]:
        """Aggregate and optionally send today's delivery summary."""
        today = dt_util.now().date().isoformat()
        records = self._records_due_today()
        if not records:
            return {"sent": False, "reason": "no_packages_today", "date": today}

        message = self._format_morning_summary(records)
        keys = [record["key"] for record in records if record.get("key")]

        if notify:
            await self._async_send_text(message)

        notifications = self.data["notifications"]
        notifications["morning_summary"][today] = keys
        await self._async_save()

        return {
            "sent": bool(notify),
            "date": today,
            "count": len(records),
            "message": message,
            "keys": keys,
        }

    async def async_send_pickup_summary(self, *, notify: bool) -> dict[str, Any]:
        """Aggregate and optionally send all outstanding pickup packages."""
        today = dt_util.now().date().isoformat()
        records = self._outstanding_pickup_records()
        if not records:
            return {"sent": False, "reason": "no_pickups_waiting", "date": today}

        message = self._format_pickup_summary(records)
        keys = [record["key"] for record in records if record.get("key")]

        if notify:
            await self._async_send_text(message)

        notifications = self.data["notifications"]
        notifications.setdefault("pickup_summary", {})[today] = keys
        await self._async_save()

        return {
            "sent": bool(notify),
            "date": today,
            "count": len(records),
            "message": message,
            "keys": keys,
        }

    async def async_mark_picked_up(self, *, package_key: str) -> dict[str, Any]:
        """Mark one stored pickup package as picked up."""
        packages: dict[str, dict[str, Any]] = self.data["packages"]
        if package_key not in packages:
            return {"updated": False, "reason": "unknown_package_key", "package_key": package_key}

        now = dt_util.now().isoformat()
        record = dict(packages[package_key])
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        record["status"] = STATUS_PICKED_UP
        record["expected_date"] = None
        record["delivery_window_start"] = None
        record["delivery_window_end"] = None
        record["extra"] = {**extra, "picked_up_at": now}

        stored = await self._async_store_records([record], notify=False)
        return {"updated": bool(stored), "package_key": package_key, "stored": stored}

    async def async_set_status(self, *, package_key: str, status: str, notify: bool = False) -> dict[str, Any]:
        """Set a stored package status from the dashboard."""
        packages: dict[str, dict[str, Any]] = self.data["packages"]
        if package_key not in packages:
            return {"updated": False, "reason": "unknown_package_key", "package_key": package_key}

        now = dt_util.now().isoformat()
        record = dict(packages[package_key])
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        record["status"] = _status_slug(status)
        if record["status"] in (STATUS_DELIVERED, STATUS_PICKED_UP, "cancelled"):
            record["expected_date"] = None
            record["delivery_window_start"] = None
            record["delivery_window_end"] = None
            record["pickup_location"] = None
            record["pickup_code"] = None
            timestamp_key = {
                STATUS_DELIVERED: "delivered_at",
                STATUS_PICKED_UP: "picked_up_at",
                "cancelled": "cancelled_at",
            }.get(record["status"], "status_changed_at")
            extra[timestamp_key] = now
        elif record["status"] == STATUS_READY_FOR_PICKUP:
            record["delivery_window_start"] = None
            record["delivery_window_end"] = None
        elif record["status"] in (STATUS_IN_TRANSIT, STATUS_EXPECTED_TODAY, STATUS_UNKNOWN):
            record["pickup_location"] = None
            record["pickup_code"] = None
        record["extra"] = {**extra, "status_changed_at": now}

        stored = await self._async_store_records([record], notify=notify)
        return {"updated": bool(stored), "package_key": package_key, "status": record["status"], "stored": stored}

    async def async_delete_package(self, *, package_key: str) -> dict[str, Any]:
        """Permanently delete one stored package record."""
        packages: dict[str, dict[str, Any]] = self.data["packages"]
        if package_key not in packages:
            return {"deleted": False, "reason": "unknown_package_key", "package_key": package_key}

        deleted = packages.pop(package_key)
        _remove_package_key_from_notifications(self.data.get("notifications"), package_key)
        self.data["last_updated"] = dt_util.now().isoformat()
        await self._async_save()
        self.hass.bus.async_fire(
            "package_inbox_updated",
            {"deleted": [package_key], "count": 1},
        )
        return {"deleted": True, "package_key": package_key, "record": _normalize_record(deleted)}

    def delivery_snapshot(self, *, margin_minutes: int = DEFAULT_WINDOW_MARGIN_MINUTES) -> dict[str, Any]:
        """Return current package delivery-window status for HA entities."""
        return build_delivery_snapshot(
            self._records_due_today(),
            now=dt_util.now(),
            margin_minutes=margin_minutes,
        )

    def dashboard_snapshot(self, *, history_limit: int = 30) -> dict[str, Any]:
        """Return active and historical package records for dashboards."""
        records: list[dict[str, Any]] = []
        records.extend(self.data["packages"].values())
        records.extend(self._postnl_records())

        normalized_records: list[dict[str, Any]] = []
        for record in records:
            normalized = _normalize_record(record)
            normalized.setdefault("key", stable_key(normalized))
            if not _record_exclusion_reason(normalized):
                normalized_records.append(normalized)

        return build_dashboard_snapshot(
            normalized_records,
            delivery_snapshot=self.delivery_snapshot(
                margin_minutes=DEFAULT_WINDOW_MARGIN_MINUTES,
            ),
            now=dt_util.now(),
            history_limit=history_limit,
        )

    async def _async_store_records(
        self,
        records: list[dict[str, Any]],
        *,
        notify: bool,
    ) -> list[str]:
        stored_keys: list[str] = []
        packages: dict[str, dict[str, Any]] = self.data["packages"]
        now = dt_util.now().isoformat()

        for record in records:
            normalized = _normalize_record(record)
            normalized = apply_vinted_cross_reference(normalized, packages)
            key = normalized.get("key") or stable_key(normalized)
            normalized["key"] = key
            normalized["updated_at"] = now

            previous = packages.get(key, {})
            if previous:
                normalized["created_at"] = previous.get("created_at") or now
                normalized["notified"] = previous.get("notified", {})
                normalized["history"] = _append_history(previous, normalized)
            else:
                normalized["created_at"] = now
                normalized["notified"] = {}
                normalized["history"] = []

            packages[key] = normalized
            stored_keys.append(key)

            if notify:
                await self._async_maybe_notify_record(normalized)

        reconciled_keys = reconcile_vinted_carrier_links(packages)
        for key in reconciled_keys:
            if key in packages:
                packages[key]["updated_at"] = now
            if key not in stored_keys:
                stored_keys.append(key)

        if stored_keys:
            self.data["last_updated"] = now
            await self._async_save()
            self.hass.bus.async_fire(
                "package_inbox_updated",
                {"keys": stored_keys, "count": len(stored_keys)},
            )

        return stored_keys

    def _tracking_skip_reason(self, record: dict[str, Any], *, force: bool) -> str | None:
        if not record.get("tracking_code"):
            return "missing_tracking_code"
        if force:
            return None
        if record.get("status") in (STATUS_DELIVERED, STATUS_PICKED_UP, "cancelled"):
            return "terminal_status"

        last_checked = dt_util.parse_datetime(str(record.get("tracking_last_checked") or ""))
        if not last_checked:
            return None

        age = dt_util.now().timestamp() - last_checked.timestamp()
        refresh_seconds = int(self.config[CONF_TRACKING_REFRESH_MINUTES]) * 60
        if age < refresh_seconds:
            return "checked_recently"
        return None

    async def _async_tracking_update_for_record(self, record: dict[str, Any]) -> dict[str, Any]:
        carrier = record.get("carrier") or "unknown"
        tracking_code = record.get("tracking_code")

        if carrier == "postnl":
            update = self._postnl_tracking_update(record)
            if update:
                return update

        if carrier == "vinted":
            return _tracking_error_update(
                record,
                "vinted_mail_is_source_of_truth",
                source="mail_only",
                supported=False,
            )

        if carrier == "amazon":
            return _tracking_error_update(
                record,
                "amazon_tracking_requires_account",
                source="mail_only",
                supported=False,
            )

        scraper_update = await self._async_fetch_tracking_scraper(record)
        if scraper_update and _tracking_update_has_delivery_detail(scraper_update):
            return scraper_update

        if carrier == "fedex":
            fedex_api_update = await self._async_fetch_fedex_public_api(record)
            if fedex_api_update and _tracking_update_has_delivery_detail(fedex_api_update):
                return fedex_api_update
            mail_update = extract_fedex_tracking_update_from_mail(
                record=record,
                error=str((fedex_api_update or {}).get("tracking_refresh_error") or TRACKING_BLOCKED_ERROR),
                today=dt_util.now().date(),
            )
            if _tracking_update_has_delivery_detail(mail_update):
                return mail_update

        if not supports_public_tracking(carrier):
            return _tracking_error_update(
                record,
                "carrier_not_supported",
                source="tracking_refresh",
                supported=False,
            )

        api_url = build_tracking_api_url(
            carrier,
            tracking_code,
            delivery_postcode=self.config.get(CONF_DELIVERY_POSTCODE),
            delivery_house_number=self.config.get(CONF_DELIVERY_HOUSE_NUMBER),
        )
        if api_url:
            api_update = await self._async_fetch_tracking_api(record, api_url)
            if api_update:
                return api_update

        url = _preferred_tracking_page_url(record, self.config)
        if not url:
            return _tracking_error_update(
                record,
                "missing_tracking_url",
                source="tracking_refresh",
                supported=False,
            )

        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(total=int(self.config[CONF_TRACKING_TIMEOUT]))
        headers = {
            "User-Agent": self.config[CONF_TRACKING_USER_AGENT],
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        }

        try:
            async with session.get(url, headers=headers, timeout=timeout) as response:
                html = await response.text(errors="replace")
                update = extract_tracking_update(
                    carrier=carrier,
                    tracking_code=str(tracking_code),
                    html=html,
                    fetched_url=str(response.url),
                    today=dt_util.now().date(),
                )
                update["tracking_refresh_url"] = str(response.url)
                if response.status >= 400:
                    update["tracking_refresh_error"] = f"http_{response.status}"
                if carrier == "fedex" and update.get("tracking_refresh_error"):
                    mail_update = extract_fedex_tracking_update_from_mail(
                        record=record,
                        error=str(update.get("tracking_refresh_error") or ""),
                        today=dt_util.now().date(),
                    )
                    if _tracking_update_has_delivery_detail(mail_update):
                        return mail_update
                if self.config.get(CONF_ENABLE_AI_FALLBACK) and _tracking_update_needs_ai(update):
                    ai_update = await self._async_ai_parse_tracking_page(
                        record,
                        page_text=html,
                        fetched_url=str(response.url),
                    )
                    if ai_update:
                        update.update(ai_update)
                        if _tracking_update_has_delivery_detail(ai_update):
                            update.pop("tracking_refresh_error", None)
                return update
        except TimeoutError:
            return _tracking_error_update(
                record,
                "tracking_request_timeout",
                source="public_tracking_page",
                supported=True,
                url=url,
            )
        except ClientError as err:
            return _tracking_error_update(
                record,
                f"tracking_request_failed: {err}",
                source="public_tracking_page",
                supported=True,
                url=url,
            )

    async def _async_fetch_tracking_api(self, record: dict[str, Any], url: str) -> dict[str, Any] | None:
        """Fetch a carrier JSON tracking endpoint when one is publicly available."""
        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(total=int(self.config[CONF_TRACKING_TIMEOUT]))
        headers = {
            "User-Agent": self.config[CONF_TRACKING_USER_AGENT],
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        }
        try:
            async with session.get(url, headers=headers, timeout=timeout, ssl=False) as response:
                text = await response.text(errors="replace")
                if response.status >= 400:
                    return _tracking_error_update(
                        record,
                        f"tracking_api_http_{response.status}",
                        source="public_tracking_api",
                        supported=True,
                        refresh_url=str(response.url),
                        url=build_tracking_url(
                            record.get("carrier"),
                            record.get("tracking_code"),
                            delivery_postcode=self.config.get(CONF_DELIVERY_POSTCODE),
                            delivery_house_number=self.config.get(CONF_DELIVERY_HOUSE_NUMBER),
                        ),
                    )
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    return None
                if payload in (None, [], {}):
                    return _tracking_error_update(
                        record,
                        "tracking_api_not_found",
                        source="public_tracking_api",
                        supported=True,
                        refresh_url=str(response.url),
                        url=build_tracking_url(
                            record.get("carrier"),
                            record.get("tracking_code"),
                            delivery_postcode=self.config.get(CONF_DELIVERY_POSTCODE),
                            delivery_house_number=self.config.get(CONF_DELIVERY_HOUSE_NUMBER),
                        ),
                    )
                update = extract_tracking_update_from_json(
                    carrier=str(record.get("carrier") or "unknown"),
                    tracking_code=str(record.get("tracking_code") or ""),
                    payload=payload,
                    fetched_url=str(response.url),
                    today=dt_util.now().date(),
                )
                update["tracking_refresh_url"] = str(response.url)
                update["tracking_url"] = record.get("tracking_url") or update.get("tracking_url")
                return update
        except TimeoutError:
            return _tracking_error_update(
                record,
                "tracking_api_timeout",
                source="public_tracking_api",
                supported=True,
                refresh_url=url,
                url=build_tracking_url(
                    record.get("carrier"),
                    record.get("tracking_code"),
                    delivery_postcode=self.config.get(CONF_DELIVERY_POSTCODE),
                    delivery_house_number=self.config.get(CONF_DELIVERY_HOUSE_NUMBER),
                ),
            )
        except ClientError as err:
            _LOGGER.debug("Tracking API failed for %s: %s", record.get("key"), err)
            return None

    async def _async_fetch_fedex_public_api(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Fetch the public FedEx web tracker JSON endpoint as a best-effort fallback."""
        tracking_code = _clean_optional(record.get("tracking_code"))
        if not tracking_code:
            return None

        endpoint = build_fedex_tracking_api_url()
        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(total=int(self.config[CONF_TRACKING_TIMEOUT]))
        headers = {
            "User-Agent": self.config[CONF_TRACKING_USER_AGENT],
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
            "Content-Type": "application/json",
            "x-locale": "nl_NL",
        }
        try:
            async with session.post(
                endpoint,
                json=build_fedex_tracking_payload(tracking_code),
                headers=headers,
                timeout=timeout,
            ) as response:
                text = await response.text(errors="replace")
                if response.status >= 400:
                    return _tracking_error_update(
                        record,
                        f"fedex_api_http_{response.status}",
                        source="fedex_public_api",
                        supported=True,
                        url=build_tracking_url("fedex", tracking_code),
                        refresh_url=str(response.url),
                    )
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    return _tracking_error_update(
                        record,
                        "fedex_api_non_json",
                        source="fedex_public_api",
                        supported=True,
                        url=build_tracking_url("fedex", tracking_code),
                        refresh_url=str(response.url),
                    )
                update = extract_fedex_tracking_update_from_json(
                    tracking_code=tracking_code,
                    payload=payload,
                    fetched_url=str(response.url),
                    today=dt_util.now().date(),
                )
                update["tracking_refresh_url"] = str(response.url)
                return update
        except TimeoutError:
            return _tracking_error_update(
                record,
                "fedex_api_timeout",
                source="fedex_public_api",
                supported=True,
                url=build_tracking_url("fedex", tracking_code),
                refresh_url=endpoint,
            )
        except ClientError as err:
            _LOGGER.debug("FedEx public API failed for %s: %s", record.get("key"), err)
            return None

    async def _async_fetch_tracking_scraper(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Ask an optional local scraper sidecar for normalized tracking data."""
        base_url = _clean_optional(self.config.get(CONF_TRACKING_SCRAPER_URL))
        tracking_code = _clean_optional(record.get("tracking_code"))
        carrier = _carrier_slug(record.get("carrier"))
        if not base_url or not tracking_code or carrier not in TRACKING_SCRAPER_CARRIERS:
            return None

        endpoint = urljoin(base_url.rstrip("/") + "/", "track")
        tracking_url = record.get("tracking_url") or build_tracking_url(
            carrier,
            tracking_code,
            delivery_postcode=self.config.get(CONF_DELIVERY_POSTCODE),
            delivery_house_number=self.config.get(CONF_DELIVERY_HOUSE_NUMBER),
        )
        payload = {
            "carrier": carrier,
            "tracking_code": tracking_code,
            "tracking_url": tracking_url,
            "package_key": record.get("key"),
            "delivery_postcode": self.config.get(CONF_DELIVERY_POSTCODE),
            "delivery_house_number": self.config.get(CONF_DELIVERY_HOUSE_NUMBER),
        }
        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(total=int(self.config[CONF_TRACKING_TIMEOUT]))
        headers = {
            "User-Agent": self.config[CONF_TRACKING_USER_AGENT],
            "Accept": "application/json",
        }
        token = _clean_optional(self.config.get(CONF_TRACKING_SCRAPER_TOKEN))
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with session.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as response:
                text = await response.text(errors="replace")
                if response.status >= 400:
                    _LOGGER.debug("Tracking scraper returned HTTP %s for %s", response.status, record.get("key"))
                    return None
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    _LOGGER.debug("Tracking scraper returned non-JSON for %s", record.get("key"))
                    return None
                update = normalize_tracking_scraper_update(
                    payload,
                    carrier=carrier,
                    tracking_code=tracking_code,
                    tracking_url=tracking_url,
                    today=dt_util.now().date(),
                )
                if update:
                    update["tracking_refresh_url"] = endpoint
                return update
        except TimeoutError:
            _LOGGER.debug("Tracking scraper timed out for %s", record.get("key"))
            return None
        except ClientError as err:
            _LOGGER.debug("Tracking scraper failed for %s: %s", record.get("key"), err)
            return None

    async def _async_fetch_vinted_sidecar_records(self) -> dict[str, Any] | None:
        """Mirror normalized Vinted parcels from the optional local browser sidecar."""
        base_url = _clean_optional(self.config.get(CONF_TRACKING_SCRAPER_URL))
        if not base_url:
            return None

        endpoint = urljoin(base_url.rstrip("/") + "/", "parcels/vinted")
        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(total=int(self.config[CONF_TRACKING_TIMEOUT]))
        headers = {
            "User-Agent": self.config[CONF_TRACKING_USER_AGENT],
            "Accept": "application/json",
        }
        token = _clean_optional(self.config.get(CONF_TRACKING_SCRAPER_TOKEN))
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with session.get(endpoint, headers=headers, timeout=timeout) as response:
                text = await response.text(errors="replace")
                if response.status >= 400:
                    return {
                        "status": "error",
                        "supported": True,
                        "error": f"vinted_sidecar_http_{response.status}",
                        "tracking_refresh_url": str(response.url),
                        "records": [],
                    }
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    return {
                        "status": "error",
                        "supported": True,
                        "error": "vinted_sidecar_non_json",
                        "tracking_refresh_url": str(response.url),
                        "records": [],
                    }
        except TimeoutError:
            return {
                "status": "error",
                "supported": True,
                "error": "vinted_sidecar_timeout",
                "tracking_refresh_url": endpoint,
                "records": [],
            }
        except ClientError as err:
            _LOGGER.debug("Vinted sidecar failed: %s", err)
            return None

        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return {
                "status": "error",
                "supported": True,
                "error": "vinted_sidecar_missing_records",
                "tracking_refresh_url": endpoint,
                "records": [],
            }

        normalized_records: list[dict[str, Any]] = []
        checked_at = dt_util.now().isoformat()
        for record in records:
            if not isinstance(record, dict):
                continue
            normalized = _normalize_record(
                {
                    **record,
                    "carrier": record.get("carrier") or "vinted",
                    "shop": record.get("shop") or "Vinted",
                    "source": record.get("source") or "vinted_sidecar",
                    "confidence": record.get("confidence") or "high",
                    "tracking_refresh_source": "vinted_sidecar",
                    "tracking_refresh_supported": True,
                    "tracking_last_checked": checked_at,
                }
            )
            if not _record_exclusion_reason(normalized):
                normalized_records.append(normalized)

        return {
            "status": payload.get("status") or "ok",
            "supported": True,
            "error": payload.get("error"),
            "tracking_refresh_url": endpoint,
            "records": normalized_records,
            "accounts": payload.get("accounts"),
        }

    def _postnl_tracking_update(self, record: dict[str, Any]) -> dict[str, Any] | None:
        state = self.hass.states.get(self.config[CONF_POSTNL_DELIVERY_SENSOR])
        if state is None:
            return None

        tracking_code = str(record.get("tracking_code") or "").lower()
        record_shop = clean_text(str(record.get("shop") or "")).lower()
        for item in state.attributes.get("enroute") or []:
            if not isinstance(item, dict):
                continue
            item_code = str(_postnl_item_tracking_code(item) or "").lower()
            if tracking_code:
                if not item_code or tracking_code != item_code:
                    continue
            elif record_shop:
                item_text = clean_text(
                    " ".join(
                        str(item.get(key) or "")
                        for key in ("name", "sender", "description", "status_message")
                    )
                ).lower()
                if record_shop not in item_text and item_text not in record_shop:
                    continue
            else:
                continue

            planned_from = item.get("planned_from") or item.get("expected_datetime")
            planned_to = item.get("planned_to")
            planned_date = _date_from_value(planned_from or item.get("planned_date") or planned_to)
            status_text = item.get("status_message") or item.get("status") or item.get("phase") or item.get("description")
            return {
                "carrier": "postnl",
                "shop": item.get("name") or item.get("sender") or item.get("description") or record.get("shop"),
                "tracking_code": record.get("tracking_code") or item_code.upper() or None,
                "status": _postnl_status_from_text(status_text, planned_date),
                "expected_date": planned_date,
                "delivery_window_start": _time_from_value(planned_from),
                "delivery_window_end": _time_from_value(planned_to),
                "tracking_status_text": status_text,
                "tracking_url": item.get("url"),
                "tracking_refresh_source": "postnl_integration",
                "tracking_refresh_supported": True,
            }
        return None

    async def _async_maybe_notify_record(self, record: dict[str, Any]) -> None:
        key = record.get("key")
        if not key:
            return

        if record.get("status") == STATUS_READY_FOR_PICKUP:
            notified = self.data["notifications"]["pickup_notified"]
            if key not in notified:
                await self._async_notify_pickup(record)
                notified.append(key)
            return

        if self._record_due_today(record) and _after_morning_cutoff():
            notified = self.data["notifications"]["extra_today_notified"]
            summary_keys = self.data["notifications"]["morning_summary"].get(
                dt_util.now().date().isoformat(),
                [],
            )
            if key not in notified and key not in summary_keys:
                await self._async_notify_extra_today(record)
                notified.append(key)

    async def _async_notify_pickup(self, record: dict[str, Any]) -> None:
        carrier = _carrier_title(record.get("carrier"))
        location = record.get("pickup_location")
        code = record.get("pickup_code")
        shop = record.get("shop")
        display = _notification_package_title(record)

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

        await self._async_send_text("\n\n".join([lines[0], "\n".join(lines[1:])]) if len(lines) > 1 else lines[0])

        qr_file_path = record.get("qr_file_path")
        if qr_file_path:
            await self._async_send_media(
                qr_file_path,
                message=f"{carrier} afhaal QR-code",
                mime_type=mimetypes.guess_type(qr_file_path)[0] or "image/png",
            )

    async def _async_notify_extra_today(self, record: dict[str, Any]) -> None:
        carrier = _carrier_title(record.get("carrier"))
        shop = record.get("shop") or "onbekend"
        expected = self._format_record_expected(record)
        message = f"Er is nog een pakket voor vandaag gevonden\n\nVan: {shop}\nVerwacht: {expected or carrier + ' vandaag'}"
        await self._async_send_text(message)

    async def _async_send_text(self, message: str) -> None:
        service = self.config[CONF_NOTIFY_SCRIPT]
        if "." not in service:
            raise HomeAssistantError(f"Invalid notify script service: {service}")
        domain, service_name = service.split(".", 1)
        await self.hass.services.async_call(
            domain,
            service_name,
            {"message": message},
            blocking=True,
        )

    async def _async_send_media(self, file_path: str, *, message: str, mime_type: str) -> None:
        room_id = self.config.get(CONF_MATRIX_ROOM_ID)
        if not room_id or not self.hass.services.has_service("matrix_chat", "send_media"):
            _LOGGER.debug("Skipping pickup media notification because Matrix media delivery is not configured")
            return
        await self.hass.services.async_call(
            "matrix_chat",
            "send_media",
            {
                "targets": [room_id],
                "file_path": file_path,
                "message": message,
                "mime_type": mime_type,
            },
            blocking=True,
        )

    async def _async_mark_seen(self, entry_id: str, uid: str) -> None:
        try:
            await self.hass.services.async_call(
                "imap",
                "seen",
                {"entry": entry_id, "uid": uid},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.warning("Could not mark IMAP uid %s seen: %s", uid, err)

    async def _async_store_rejection(
        self,
        fields: dict[str, str],
        classification: dict[str, Any],
    ) -> None:
        rejected = self.data.setdefault("rejected_messages", {})
        key = stable_key(
            {
                "carrier": "rejected",
                "message_id": fields.get("message_id"),
                "imap_uid": fields.get("imap_uid"),
                "raw_excerpt": fields.get("subject") or fields.get("sender"),
            }
        )
        rejected[key] = {
            "rejected_at": dt_util.now().isoformat(),
            "subject": fields.get("subject"),
            "sender": fields.get("sender"),
            "category": classification.get("category"),
            "reason": classification.get("reason"),
            "confidence": classification.get("confidence"),
            "ai_is_package": classification.get("is_package"),
        }
        self.data["last_updated"] = dt_util.now().isoformat()
        await self._async_save()

    async def _async_ai_classify_email(self, fields: dict[str, str]) -> dict[str, Any] | None:
        raw_text = "\n".join(
            value
            for value in (fields.get("subject"), fields.get("sender"), fields.get("text"))
            if value
        )
        redacted, _ = _redact_for_ai(raw_text[:8000])
        sender_identity = _sender_identity(fields.get("sender"))
        instructions = (
            "Je bent een filter voor een Home Assistant pakketmelder. "
            "Bepaal of deze e-mail echt over een pakket of afhaalpakket gaat. "
            "Een pakket is PostNL/DHL/DPD/GLS/FedEx/Chronopost/UPS/Trunkrs/Homerr/Cycloon/Instabox/"
            "TransMission/Dachser/Dynalogic/GOFO/Dragonfly/Amazon/Vinted verzending, track-and-trace, bezorging van een webwinkelpakket, "
            "een afhaalcode/QR voor een pakketpunt, of een apotheek/medicijnbestelling die met code opgehaald moet worden. "
            "NIET als pakket tellen: Picnic/AH/Jumbo/Crisp/Flink boodschappenbezorging, maaltijdbezorging, gewone winkelbestellingen "
            "zonder verzendinformatie, nieuwsbrieven, supportberichten, retourdiscussies of Vinted chat/supportberichten zonder verzending. "
            "Let sterk op afzenderidentiteit en domein. Return ONLY geldige JSON.\n\n"
            "JSON-vorm: {\"is_package\":true|false,\"category\":\"parcel|pickup|medicine_pickup|grocery_delivery|food_delivery|order_only|support|newsletter|other\","
            f"\"carrier\":\"{AI_CARRIER_LIST}|null\",\"shop\":null,"
            "\"confidence\":\"low|medium|high\",\"reason\":\"korte reden\"}\n\n"
            f"Afzenderidentiteit: {sender_identity}\n"
            f"E-mail:\n{redacted}"
        )

        try:
            response = await self.hass.services.async_call(
                "ai_task",
                "generate_data",
                {
                    "entity_id": self.config[CONF_AI_TASK_ENTITY],
                    "task_name": "package_inbox_classify_email",
                    "instructions": instructions,
                    "structure": {
                        "is_package": {"selector": {"boolean": {}}, "required": True},
                        "category": {"selector": {"text": {}}, "required": True},
                        "carrier": {"selector": {"text": {}}, "required": False},
                        "shop": {"selector": {"text": {}}, "required": False},
                        "confidence": {"selector": {"text": {}}, "required": True},
                        "reason": {"selector": {"text": {}}, "required": True},
                    },
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.warning("AI package classification failed: %s", err)
            return None

        data = response.get("data") if isinstance(response, dict) else response
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None

        is_package = data.get("is_package")
        if isinstance(is_package, str):
            is_package = is_package.strip().lower() in {"true", "yes", "ja", "1"}
        if not isinstance(is_package, bool):
            return None

        return {
            "is_package": is_package,
            "category": _clean_optional(data.get("category")) or "other",
            "carrier": _clean_optional(data.get("carrier")),
            "shop": _clean_optional(data.get("shop")),
            "confidence": _clean_optional(data.get("confidence")) or "medium",
            "reason": _clean_optional(data.get("reason")) or "",
        }

    async def _async_ai_parse(self, fields: dict[str, str]) -> list[dict[str, Any]]:
        raw_text = "\n".join(
            value
            for value in (fields.get("subject"), fields.get("sender"), fields.get("text"))
            if value
        )
        redacted, placeholders = _redact_for_ai(raw_text[:12000])
        instructions = (
            "Je extraheert pakketinformatie uit een Nederlandse/Engelse e-mail. "
            "Return ONLY valid JSON. Gebruik null als iets onbekend is. "
            "Normale track-and-trace codes mogen in tracking_code. "
            "Apotheek- of medicijnbestellingen die met een code opgehaald moeten worden zijn ready_for_pickup met carrier apotheek. "
            "Gebruik status: expected_today, in_transit, ready_for_pickup, delivered, picked_up, cancelled of unknown.\n\n"
            f"JSON-vorm: {{\"packages\":[{{\"carrier\":\"{AI_CARRIER_LIST}\","
            "\"shop\":null,\"tracking_code\":null,\"status\":\"unknown\",\"expected_date\":null,"
            "\"delivery_window_start\":null,\"delivery_window_end\":null,\"pickup_location\":null,"
            "\"pickup_code\":null,\"confidence\":\"low|medium|high\"}]}\n\n"
            f"E-mail:\n{redacted}"
        )

        try:
            response = await self.hass.services.async_call(
                "ai_task",
                "generate_data",
                {
                    "entity_id": self.config[CONF_AI_TASK_ENTITY],
                    "task_name": "package_inbox_parse_email",
                    "instructions": instructions,
                    "structure": {"packages": {"selector": {"object": {}}, "required": True}},
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.warning("AI package parse failed: %s", err)
            return []

        data = response.get("data") if isinstance(response, dict) else response
        packages = data.get("packages") if isinstance(data, dict) else None
        if not isinstance(packages, list):
            return []

        records: list[dict[str, Any]] = []
        for package in packages:
            if not isinstance(package, dict):
                continue
            record = _normalize_record(package)
            record["source"] = "ai_fallback"
            record["message_id"] = fields.get("message_id")
            record["imap_uid"] = fields.get("imap_uid")
            record["raw_excerpt"] = clean_text(raw_text)[:500]
            _restore_placeholders(record, placeholders)
            records.append(record)
        return records

    async def _async_ai_parse_tracking_page(
        self,
        record: dict[str, Any],
        *,
        page_text: str,
        fetched_url: str,
    ) -> dict[str, Any] | None:
        raw_text = clean_text(page_text[:12000])
        if is_blocked_tracking_text(raw_text):
            return {
                "tracking_refresh_source": "public_tracking_page",
                "tracking_refresh_supported": True,
                "tracking_refresh_error": TRACKING_BLOCKED_ERROR,
                "tracking_status_text": "",
            }

        redacted, placeholders = _redact_for_ai(raw_text)
        instructions = (
            "Je extraheert track-and-trace informatie uit een DHL/DPD/GLS/FedEx/Chronopost trackingpagina. "
            "Gebruik alleen informatie die expliciet op de pagina staat. Verzin niets. "
            "Return ONLY valid JSON. Gebruik null als iets onbekend is. "
            "Gebruik status: expected_today, in_transit, ready_for_pickup, delivered, picked_up, cancelled of unknown. "
            "Tijden moeten HH:MM zijn. expected_date moet YYYY-MM-DD zijn.\n\n"
            "JSON-vorm: {\"status\":\"unknown\",\"expected_date\":null,"
            "\"delivery_window_start\":null,\"delivery_window_end\":null,"
            "\"pickup_location\":null,\"tracking_status_text\":null,\"confidence\":\"low|medium|high\"}\n\n"
            f"Carrier: {record.get('carrier')}\n"
            f"Trackingcode: {_redact_tracking_code(record.get('tracking_code'))}\n"
            f"URL: {_redact_tracking_url_for_ai(fetched_url)}\n"
            f"Pagina:\n{redacted}"
        )

        try:
            response = await self.hass.services.async_call(
                "ai_task",
                "generate_data",
                {
                    "entity_id": self.config[CONF_AI_TASK_ENTITY],
                    "task_name": "package_inbox_parse_tracking_page",
                    "instructions": instructions,
                    "structure": {
                        "status": {"selector": {"text": {}}, "required": True},
                        "expected_date": {"selector": {"text": {}}, "required": False},
                        "delivery_window_start": {"selector": {"text": {}}, "required": False},
                        "delivery_window_end": {"selector": {"text": {}}, "required": False},
                        "pickup_location": {"selector": {"text": {}}, "required": False},
                        "tracking_status_text": {"selector": {"text": {}}, "required": False},
                        "confidence": {"selector": {"text": {}}, "required": True},
                    },
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.warning("AI tracking parse failed: %s", err)
            return None

        data = response.get("data") if isinstance(response, dict) else response
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None

        update = _normalize_record(
            {
                "carrier": record.get("carrier"),
                "tracking_code": record.get("tracking_code"),
                "status": data.get("status") or STATUS_UNKNOWN,
                "expected_date": data.get("expected_date"),
                "delivery_window_start": data.get("delivery_window_start"),
                "delivery_window_end": data.get("delivery_window_end"),
                "pickup_location": data.get("pickup_location"),
                "tracking_status_text": data.get("tracking_status_text"),
                "confidence": data.get("confidence") or "medium",
            }
        )
        _restore_placeholders(update, placeholders)

        result = {
            "tracking_refresh_source": "ai_tracking_page",
            "tracking_refresh_supported": True,
            "confidence": update.get("confidence"),
        }
        for key in (
            "status",
            "expected_date",
            "delivery_window_start",
            "delivery_window_end",
            "pickup_location",
            "tracking_status_text",
        ):
            value = update.get(key)
            if value and not (key == "status" and value == STATUS_UNKNOWN):
                if key == "tracking_status_text" and is_blocked_tracking_text(str(value)):
                    continue
                result[key] = value
        return result

    async def _async_extract_qr_files(
        self,
        fetched: dict[str, Any],
        text: str,
        uid: str,
    ) -> list[str]:
        images = _extract_embedded_images(fetched, text)
        saved: list[str] = []
        public_dir = self.config[CONF_PUBLIC_QR_DIR].strip("/") or DEFAULT_PUBLIC_QR_DIR
        runtime_dir = Path(self.hass.config.path("www", public_dir))
        for index, image in enumerate(images[:3], start=1):
            suffix = image["suffix"]
            digest = hashlib.sha1(image["content"]).hexdigest()[:12]
            filename = f"imap_{_safe_filename(uid)}_{index}_{digest}.{suffix}"
            runtime_path = runtime_dir / filename

            def _write() -> None:
                runtime_dir.mkdir(parents=True, exist_ok=True)
                runtime_path.write_bytes(image["content"])

            await self.hass.async_add_executor_job(_write)
            saved.append(f"/config/www/{public_dir}/{filename}")

        for index, url in enumerate(_extract_qr_urls(fetched, text)[:3], start=len(saved) + 1):
            try:
                image = await self._async_download_qr_image(url)
            except Exception as err:
                _LOGGER.warning("Could not download QR image from %s: %s", url, err)
                continue
            if not image:
                continue

            suffix = image["suffix"]
            digest = hashlib.sha1(image["content"]).hexdigest()[:12]
            filename = f"imap_{_safe_filename(uid)}_remote_{index}_{digest}.{suffix}"
            runtime_path = runtime_dir / filename

            def _write_remote() -> None:
                runtime_dir.mkdir(parents=True, exist_ok=True)
                runtime_path.write_bytes(image["content"])

            await self.hass.async_add_executor_job(_write_remote)
            saved.append(f"/config/www/{public_dir}/{filename}")
        return saved

    async def _async_download_qr_image(self, url: str) -> dict[str, Any] | None:
        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(total=int(self.config[CONF_TRACKING_TIMEOUT]))
        headers = {
            "User-Agent": self.config[CONF_TRACKING_USER_AGENT],
            "Accept": "image/png,image/jpeg,image/svg+xml,image/*,*/*;q=0.8",
        }
        async with session.get(url, headers=headers, timeout=timeout, ssl=False) as response:
            if response.status >= 400:
                return None
            content = await response.read()
            if not 100 <= len(content) <= 5_000_000:
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            suffix = "png"
            if "jpeg" in content_type or content.startswith(b"\xff\xd8"):
                suffix = "jpg"
            elif "svg" in content_type or content.lstrip().startswith(b"<svg"):
                suffix = "svg"
            elif "gif" in content_type or content.startswith(b"GIF"):
                suffix = "gif"
            return {"suffix": suffix, "content": content}

    def _records_due_today(self) -> list[dict[str, Any]]:
        records = list(self.data["packages"].values())
        records.extend(self._postnl_records())

        deduped: dict[str, dict[str, Any]] = {}
        for record in records:
            normalized = _normalize_record(record)
            normalized.setdefault("key", stable_key(normalized))
            if not _record_exclusion_reason(normalized) and self._record_due_today(normalized):
                deduped[normalized["key"]] = normalized
        return sorted(dedupe_delivery_records(list(deduped.values())), key=_sort_record)

    def _record_due_today(self, record: dict[str, Any]) -> bool:
        if record.get("status") in (STATUS_READY_FOR_PICKUP, STATUS_PICKED_UP, STATUS_DELIVERED, "cancelled"):
            return False
        today = dt_util.now().date().isoformat()
        expected_date = record.get("expected_date")
        if expected_date:
            return expected_date == today
        if record.get("status") != STATUS_EXPECTED_TODAY:
            return False
        record_date = _date_from_value(record.get("created_at") or record.get("updated_at"))
        return record_date == today

    def _outstanding_pickup_records(self) -> list[dict[str, Any]]:
        records = list(self.data["packages"].values())

        deduped: dict[str, dict[str, Any]] = {}
        for record in records:
            normalized = _normalize_record(record)
            normalized.setdefault("key", stable_key(normalized))
            if (
                not _record_exclusion_reason(normalized)
                and normalized.get("status") == STATUS_READY_FOR_PICKUP
            ):
                deduped[normalized["key"]] = normalized
        return sorted(deduped.values(), key=_sort_pickup_record)

    def _postnl_records(self) -> list[dict[str, Any]]:
        entity_id = self.config[CONF_POSTNL_DELIVERY_SENSOR]
        state = self.hass.states.get(entity_id)
        if state is None:
            return []
        enroute = state.attributes.get("enroute") or []
        if not isinstance(enroute, list):
            return []

        records: list[dict[str, Any]] = []
        for item in enroute:
            if not isinstance(item, dict):
                continue
            shop = item.get("name") or item.get("sender") or item.get("description")
            if _text_exclusion_reason(str(shop or "")):
                continue
            planned_from = item.get("planned_from") or item.get("expected_datetime")
            planned_to = item.get("planned_to")
            planned_date = _date_from_value(planned_from or item.get("planned_date") or planned_to)
            tracking_code = _postnl_item_tracking_code(item)
            records.append(
                {
                    "carrier": "postnl",
                    "shop": shop,
                    "tracking_code": tracking_code,
                    "status": STATUS_EXPECTED_TODAY if planned_date == dt_util.now().date().isoformat() else STATUS_IN_TRANSIT,
                    "expected_date": planned_date,
                    "delivery_window_start": _time_from_value(planned_from),
                    "delivery_window_end": _time_from_value(planned_to),
                    "tracking_status_text": item.get("status_message") or item.get("status") or item.get("description"),
                    "tracking_url": item.get("url"),
                    "source": "postnl_integration",
                    "confidence": "high",
                }
            )
        return records

    def _format_morning_summary(self, records: list[dict[str, Any]]) -> str:
        count = len(records)
        first = (
            "Er komt vandaag 1 pakket aan"
            if count == 1
            else f"Er komen vandaag {count} pakketten aan"
        )
        shops = _unique(
            str(record.get("shop") or "").strip()
            for record in records
            if str(record.get("shop") or "").strip()
        )
        expected = _unique(
            part
            for record in records
            if (part := self._format_record_expected(record))
        )

        lines = [first, "", f"Van: {','.join(shops) if shops else 'onbekend'}"]
        if expected:
            lines.append(f"Verwacht: {'; '.join(expected)}")
        return "\n".join(lines)

    def _format_pickup_summary(self, records: list[dict[str, Any]]) -> str:
        count = len(records)
        first = (
            "Er ligt 1 pakket klaar om op te halen"
            if count == 1
            else f"Er liggen {count} pakketten klaar om op te halen"
        )

        lines = [first]
        for record in records:
            shop = record.get("shop") or _carrier_title(record.get("carrier"))
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

    def _format_record_expected(self, record: dict[str, Any]) -> str | None:
        if record.get("status") == STATUS_READY_FOR_PICKUP:
            return None
        carrier = _carrier_title(record.get("carrier"))
        start, end = _valid_delivery_window(record)
        if start and end:
            return f"{carrier} vandaag tussen {start} en {end}"
        if record.get("expected_date") == dt_util.now().date().isoformat() or record.get("status") == STATUS_EXPECTED_TODAY:
            return f"{carrier} vandaag"
        return None

    def _format_record_pickup(self, record: dict[str, Any]) -> str | None:
        if record.get("status") != STATUS_READY_FOR_PICKUP:
            return None
        carrier = _carrier_title(record.get("carrier"))
        location = record.get("pickup_location")
        code = record.get("pickup_code")
        text = f"{carrier}"
        if location:
            text += f" bij {location}"
        if code:
            text += f" code {code}"
        return text

    async def _async_save(self) -> None:
        await self.store.async_save(self.data)


def _message_fields(*sources: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, Any] = {}
    for source in sources:
        if isinstance(source, dict):
            merged.update(source)

    headers = _coerce_headers(merged.get("headers"))
    subject = _first_string(
        merged,
        "subject",
        "Subject",
        "mail_subject",
        "title",
        default=headers.get("subject", ""),
    )
    sender = _first_string(
        merged,
        "sender",
        "from",
        "From",
        "mail_from",
        default=headers.get("from", ""),
    )
    message_id = _first_string(
        merged,
        "message_id",
        "Message-ID",
        "message-id",
        default=headers.get("message-id", ""),
    )

    text_parts: list[str] = []
    for key in ("text", "body", "plain", "html", "message", "content", "payload", "raw"):
        value = merged.get(key)
        if isinstance(value, str):
            text_parts.append(value)
    if not text_parts:
        text_parts.append(_stringify_text(merged))

    imap_uid = str(merged.get("uid") or merged.get("imap_uid") or "")
    entry_id = str(merged.get("entry_id") or merged.get("entry") or "")
    if entry_id and imap_uid and ":" not in imap_uid:
        imap_uid = f"{entry_id}:{imap_uid}"

    return {
        "subject": subject,
        "sender": sender,
        "message_id": message_id,
        "text": clean_text("\n".join(text_parts)),
        "imap_uid": imap_uid,
    }


def _coerce_headers(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k).lower(): str(v) for k, v in value.items()}
    if isinstance(value, list):
        headers: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("key") or "").lower()
                val = str(item.get("value") or "")
                if name:
                    headers[name] = val
            elif isinstance(item, str) and ":" in item:
                name, val = item.split(":", 1)
                headers[name.lower().strip()] = val.strip()
        return headers
    if isinstance(value, str):
        headers = {}
        for line in value.splitlines():
            if ":" in line:
                name, val = line.split(":", 1)
                headers[name.lower().strip()] = val.strip()
        return headers
    return {}


def _first_string(source: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return clean_text(value)
    return clean_text(default)


async def _async_json_or_text(response: Any) -> Any:
    text = await response.text(errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _stringify_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []
        for key, child in value.items():
            if key in {"headers", "attachments"}:
                continue
            parts.append(_stringify_text(child))
        return "\n".join(parts)
    if isinstance(value, list):
        return "\n".join(_stringify_text(item) for item in value)
    return ""


def _needs_ai_fallback(records: list[dict[str, Any]], fields: dict[str, str]) -> bool:
    if not is_likely_package_email(fields.get("subject"), fields.get("sender"), fields.get("text")):
        return False
    if not records:
        return True
    return all(record.get("confidence") == "low" for record in records)


def _tracking_update_needs_ai(update: dict[str, Any]) -> bool:
    if update.get("delivery_window_start") and update.get("delivery_window_end"):
        return False
    if update.get("tracking_refresh_error"):
        return True
    return update.get("status") in (None, STATUS_UNKNOWN)


def _tracking_update_has_delivery_detail(update: dict[str, Any]) -> bool:
    if update.get("tracking_status_text") and is_blocked_tracking_text(str(update["tracking_status_text"])):
        return False
    return bool(
        update.get("expected_date")
        or update.get("delivery_window_start")
        or update.get("delivery_window_end")
        or update.get("pickup_location")
        or update.get("status") not in (None, STATUS_UNKNOWN)
    )


def _tracking_diagnostic(key: str, record: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": key,
        "carrier": record.get("carrier") or update.get("carrier") or "unknown",
        "status": update.get("status"),
        "source": update.get("tracking_refresh_source"),
        "supported": update.get("tracking_refresh_supported"),
        "error": update.get("tracking_refresh_error"),
        "tracking_url": update.get("tracking_url") or record.get("tracking_url"),
        "tracking_api_url": update.get("tracking_api_url"),
        "tracking_refresh_url": update.get("tracking_refresh_url"),
        "has_delivery_detail": _tracking_update_has_delivery_detail(update),
    }


def _record_has_vinted_source(record: dict[str, Any]) -> bool:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return (
        _carrier_slug(record.get("carrier")) == "vinted"
        or clean_text(str(record.get("shop") or "")).lower() == "vinted"
        or clean_text(str(record.get("source") or "")).lower().startswith("vinted")
        or bool(extra.get("vinted_cross_reference"))
    )


def _notification_package_title(record: dict[str, Any]) -> str:
    if _record_has_vinted_source(record):
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        title = clean_text(str(extra.get("vinted_item_title") or record.get("item_title") or ""))
        return title or "Vinted"
    shop = clean_text(str(record.get("shop") or ""))
    if shop:
        return shop
    return _carrier_title(record.get("carrier"))


def _email_exclusion_reason(fields: dict[str, str]) -> str | None:
    sender = fields.get("sender") or ""
    subject = fields.get("subject") or ""
    text = fields.get("text") or ""
    return _text_exclusion_reason("\n".join((sender, subject, text)))


def _record_exclusion_reason(record: dict[str, Any]) -> str | None:
    haystack = "\n".join(
        str(record.get(key) or "")
        for key in ("carrier", "shop", "source", "raw_excerpt", "message_id", "imap_uid")
    )
    reason = _text_exclusion_reason(haystack)
    if reason:
        return reason
    if record.get("carrier") == "vinted":
        lowered = haystack.lower()
        if any(term in lowered for term in ("support_vinted", "not as described", "you've got a new message")) and not any(
            term in lowered
            for term in ("pakket", "package", "parcel", "zending", "tracking", "pickup", "afhalen", "ligt klaar")
        ):
            return "vinted_support_message"
    if (
        record.get("status") == STATUS_UNKNOWN
        and not record.get("tracking_code")
        and not record.get("expected_date")
        and not record.get("delivery_window_start")
        and not record.get("delivery_window_end")
        and not record.get("pickup_code")
        and not record.get("pickup_location")
    ):
        return "no_actionable_package_details"
    return None


def _text_exclusion_reason(value: str) -> str | None:
    lowered = clean_text(value).lower()
    grocery_domains = (
        "picnic.nl",
        "ah.nl",
        "jumbo.com",
        "crisp.nl",
        "flink.com",
    )
    food_domains = (
        "thuisbezorgd.nl",
        "ubereats.com",
        "deliveroo.",
    )
    if any(domain in lowered for domain in grocery_domains):
        return "grocery_delivery_not_package"
    if any(domain in lowered for domain in food_domains):
        return "food_delivery_not_package"
    if "picnic" in lowered and any(term in lowered for term in ("boodschap", "boodschappen", "bezorgslot", "bestelling")):
        return "grocery_delivery_not_package"
    return None


def _redact_for_ai(value: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def _replace_code(match: re.Match[str]) -> str:
        key = f"CODE_{len(placeholders) + 1}"
        placeholders[key] = match.group(0)
        return key

    redacted = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "EMAIL", value)
    redacted = re.sub(r"https?://\S+", "URL", redacted)
    redacted = re.sub(r"\b(?:\+31|0)\d(?:[\s-]?\d){8,}\b", "PHONE", redacted)
    redacted = re.sub(
        r"\b[A-ZÀ-ÿ][A-Za-zÀ-ÿ' -]{2,40}\s+(?:straat|laan|weg|plein|dijk|kade|kamp|hof|pad|singel)\s*\d+[A-Za-z]?\b",
        "ADDRESS",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(r"\b[A-Z0-9]{8,32}\b", _replace_code, redacted)
    return redacted, placeholders


def _redact_tracking_code(value: Any) -> str:
    text = clean_text(str(value or ""))
    return f"{text[:4]}...{text[-4:]}" if len(text) > 8 else "CODE"


def _sender_identity(sender: str | None) -> str:
    cleaned = clean_text(sender)
    if not cleaned:
        return "onbekend"
    display = re.sub(r"<[^>]+>", "", cleaned).strip(" \"'")
    domains = sorted({match.group(1).lower() for match in re.finditer(r"@([\w.-]+\.[A-Za-z]{2,})", cleaned)})
    if domains:
        return f"{display or 'onbekend'}; domein(en): {', '.join(domains)}"
    return display[:120]


def _restore_placeholders(record: dict[str, Any], placeholders: dict[str, str]) -> None:
    for field in ("tracking_code", "pickup_code"):
        value = record.get(field)
        if isinstance(value, str) and value in placeholders:
            record[field] = placeholders[value]


def _remove_package_key_from_notifications(notifications: Any, package_key: str) -> None:
    if not isinstance(notifications, dict):
        return
    for list_key in ("pickup_notified", "extra_today_notified"):
        values = notifications.get(list_key)
        if isinstance(values, list):
            notifications[list_key] = [key for key in values if key != package_key]
    for dict_key in ("morning_summary", "pickup_summary"):
        grouped = notifications.get(dict_key)
        if not isinstance(grouped, dict):
            continue
        for day, values in list(grouped.items()):
            if isinstance(values, list):
                grouped[day] = [key for key in values if key != package_key]


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "carrier": _carrier_slug(record.get("carrier")),
        "shop": _clean_optional(record.get("shop")),
        "tracking_code": _clean_optional(record.get("tracking_code")),
        "status": _status_slug(record.get("status")),
        "expected_date": _clean_optional(record.get("expected_date")),
        "delivery_window_start": _clean_optional(record.get("delivery_window_start")),
        "delivery_window_end": _clean_optional(record.get("delivery_window_end")),
        "pickup_location": _clean_optional(record.get("pickup_location")),
        "pickup_code": _clean_optional(record.get("pickup_code")),
        "qr_file_path": _clean_optional(record.get("qr_file_path")),
        "source": _clean_optional(record.get("source")) or "manual",
        "confidence": _clean_optional(record.get("confidence")) or "low",
        "message_id": _clean_optional(record.get("message_id")),
        "imap_uid": _clean_optional(record.get("imap_uid")),
        "raw_excerpt": _clean_optional(record.get("raw_excerpt")),
        "tracking_url": _clean_optional(record.get("tracking_url")),
        "tracking_api_url": _clean_optional(record.get("tracking_api_url")),
        "tracking_refresh_url": _clean_optional(record.get("tracking_refresh_url")),
        "tracking_status_text": _clean_optional(record.get("tracking_status_text")),
        "tracking_last_checked": _clean_optional(record.get("tracking_last_checked")),
        "tracking_refresh_source": _clean_optional(record.get("tracking_refresh_source")),
        "tracking_refresh_error": _clean_optional(record.get("tracking_refresh_error")),
        "tracking_refresh_supported": _bool_optional(record.get("tracking_refresh_supported")),
        "tracking_refresh_has_delivery_detail": _bool_optional(record.get("tracking_refresh_has_delivery_detail")),
        "extra": record.get("extra") if isinstance(record.get("extra"), dict) else {},
    }
    _sanitize_delivery_window(normalized)
    if record.get("key"):
        normalized["key"] = str(record["key"])
    return normalized


def _sanitize_delivery_window(record: dict[str, Any]) -> None:
    start, end = _valid_delivery_window(record)
    record["delivery_window_start"] = start
    record["delivery_window_end"] = end


def _valid_delivery_window(record: dict[str, Any]) -> tuple[str | None, str | None]:
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


def _append_history(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    history = previous.get("history") if isinstance(previous.get("history"), list) else []
    previous_snapshot = {
        "updated_at": previous.get("updated_at"),
        "status": previous.get("status"),
        "expected_date": previous.get("expected_date"),
        "delivery_window_start": previous.get("delivery_window_start"),
        "delivery_window_end": previous.get("delivery_window_end"),
        "pickup_location": previous.get("pickup_location"),
        "source": previous.get("source"),
    }
    if previous_snapshot not in history[-3:]:
        history = [*history[-9:], previous_snapshot]
    return history


def _preferred_tracking_page_url(record: dict[str, Any], config: dict[str, Any] | None = None) -> str | None:
    """Prefer the exact tracking link from the mail before a generic carrier URL."""
    existing = _clean_optional(record.get("tracking_url"))
    if existing and existing.lower().startswith(("http://", "https://")):
        return existing
    config = config or {}
    return build_tracking_url(
        record.get("carrier"),
        record.get("tracking_code"),
        delivery_postcode=config.get(CONF_DELIVERY_POSTCODE),
        delivery_house_number=config.get(CONF_DELIVERY_HOUSE_NUMBER),
    )


def _redact_tracking_url_for_ai(url: str | None) -> str:
    """Keep the carrier/path signal, but never send tracking URL tokens to AI."""
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[tracking-url]"
    if not parsed.scheme or not parsed.netloc:
        return "[tracking-url]"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _tracking_error_update(
    record: dict[str, Any],
    error: str,
    *,
    source: str,
    supported: bool,
    url: str | None = None,
    refresh_url: str | None = None,
) -> dict[str, Any]:
    return {
        "carrier": record.get("carrier") or "unknown",
        "tracking_code": record.get("tracking_code"),
        "tracking_url": url or build_tracking_url(record.get("carrier"), record.get("tracking_code")),
        "tracking_refresh_url": refresh_url or url or build_tracking_url(record.get("carrier"), record.get("tracking_code")),
        "tracking_refresh_source": source,
        "tracking_refresh_supported": supported,
        "tracking_refresh_error": error,
    }


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = clean_text(str(value))
    if text.lower() in {"null", "none", "unknown", "onbekend", "n/a", "na"}:
        return None
    return text or None


def _confidence_rank(value: Any) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(clean_text(str(value or "")).lower(), 0)


def _bool_optional(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = clean_text(str(value)).lower()
    if text in {"true", "yes", "on", "1"}:
        return True
    if text in {"false", "no", "off", "0"}:
        return False
    return None


def _carrier_slug(value: Any) -> str:
    text = clean_text(str(value or "unknown")).lower()
    normalized = normalize_carrier(text)
    if normalized != "unknown":
        return normalized
    if "post" in text and "nl" in text:
        return "postnl"
    if "benu" in text or "apotheek" in text or "pharmacy" in text:
        return "apotheek"
    for carrier in (
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
        "amazon",
        "vinted",
        "apotheek",
    ):
        if carrier in text:
            return carrier
    return "unknown" if not text else text[:32]


def _status_slug(value: Any) -> str:
    text = clean_text(str(value or "")).lower()
    aliases = {
        "expected_today": STATUS_EXPECTED_TODAY,
        "today": STATUS_EXPECTED_TODAY,
        "in_transit": STATUS_IN_TRANSIT,
        "transit": STATUS_IN_TRANSIT,
        "ready_for_pickup": STATUS_READY_FOR_PICKUP,
        "pickup": STATUS_READY_FOR_PICKUP,
        "klaar": STATUS_READY_FOR_PICKUP,
        "delivered": STATUS_DELIVERED,
        "bezorgd": STATUS_DELIVERED,
        "picked_up": STATUS_PICKED_UP,
        "picked up": STATUS_PICKED_UP,
        "opgehaald": STATUS_PICKED_UP,
        "afgehaald": STATUS_PICKED_UP,
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "geannuleerd": "cancelled",
    }
    return aliases.get(text, text or STATUS_UNKNOWN)


def _carrier_title(value: Any) -> str:
    carrier = _carrier_slug(value)
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


def _after_morning_cutoff() -> bool:
    now = dt_util.now()
    return now.time() >= time(8, 30)


def _date_from_value(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    parsed = dt_util.parse_datetime(text)
    if parsed is not None:
        return dt_util.as_local(parsed).date().isoformat() if parsed.tzinfo else parsed.date().isoformat()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _time_from_value(value: Any) -> str | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return None
    if parsed.tzinfo:
        parsed = dt_util.as_local(parsed)
    return parsed.strftime("%H:%M")


def _postnl_item_tracking_code(item: dict[str, Any]) -> str | None:
    for key in ("barcode", "tracking_code", "trackingCode"):
        value = _clean_optional(item.get(key))
        if value:
            return value.upper()
    for key in ("key", "url"):
        value = _clean_optional(item.get(key))
        if not value:
            continue
        match = re.search(r"\b(3S[A-Z0-9]{8,18})\b", value, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def _postnl_status_from_text(value: Any, planned_date: str | None) -> str:
    lowered = clean_text(str(value or "")).lower()
    if any(term in lowered for term in ("afgeleverd", "bezorgd", "delivered")):
        return STATUS_DELIVERED
    if planned_date == dt_util.now().date().isoformat():
        return STATUS_EXPECTED_TODAY
    return STATUS_IN_TRANSIT


def _sort_record(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        record.get("expected_date") or "",
        record.get("delivery_window_start") or "",
        record.get("carrier") or "",
    )


def _sort_pickup_record(record: dict[str, Any]) -> tuple[str, str, str]:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    deadline = extra.get("pickup_deadline") or record.get("pickup_deadline") or "9999-12-31"
    return (
        str(deadline),
        record.get("shop") or "",
        record.get("carrier") or "",
    )


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _extract_embedded_images(fetched: dict[str, Any], text: str) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for match in re.finditer(r"data:image/(png|jpeg|jpg|gif);base64,([A-Za-z0-9+/=\s]+)", text):
        content = _decode_base64(match.group(2))
        if content:
            images.append({"suffix": "jpg" if match.group(1) == "jpeg" else match.group(1), "content": content})

    for raw in _iter_raw_messages(fetched):
        images.extend(_images_from_raw_message(raw))

    if not images:
        images.extend(_images_from_tree(fetched))

    return [image for image in images if 100 <= len(image["content"]) <= 5_000_000]


def _extract_qr_urls(fetched: dict[str, Any], text: str) -> list[str]:
    haystack = html_lib.unescape("\n".join((text or "", _stringify_text(fetched))))
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://[^\s\"'<>]+", haystack):
        url = match.group(0).rstrip(").,;")
        lowered = url.lower()
        if "/qr_codes/" not in lowered and "qr_code" not in lowered:
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _iter_raw_messages(value: Any) -> Iterable[str | bytes]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in {"raw", "message", "email", "source"} and isinstance(child, (str, bytes)):
                yield child
            else:
                yield from _iter_raw_messages(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_raw_messages(child)


def _images_from_raw_message(raw: str | bytes) -> list[dict[str, Any]]:
    try:
        msg = (
            message_from_bytes(raw, policy=policy.default)
            if isinstance(raw, bytes)
            else message_from_string(raw, policy=policy.default)
        )
    except Exception:
        return []

    images: list[dict[str, Any]] = []
    for part in msg.walk():
        content_type = part.get_content_type()
        if not content_type.startswith("image/"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        suffix = content_type.split("/", 1)[1].replace("jpeg", "jpg")
        images.append({"suffix": suffix, "content": payload})
    return images


def _images_from_tree(value: Any) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    if isinstance(value, dict):
        content_type = str(value.get("content_type") or value.get("mime_type") or value.get("type") or "").lower()
        filename = str(value.get("filename") or value.get("name") or "").lower()
        if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".gif")):
            encoded = value.get("content") or value.get("data") or value.get("payload") or value.get("body")
            content = _decode_base64(encoded) if isinstance(encoded, str) else None
            if content:
                suffix = "png"
                if "jpeg" in content_type or filename.endswith((".jpg", ".jpeg")):
                    suffix = "jpg"
                elif "gif" in content_type or filename.endswith(".gif"):
                    suffix = "gif"
                images.append({"suffix": suffix, "content": content})
        for child in value.values():
            images.extend(_images_from_tree(child))
    elif isinstance(value, list):
        for child in value:
            images.extend(_images_from_tree(child))
    return images


def _decode_base64(value: str) -> bytes | None:
    cleaned = re.sub(r"\s+", "", value.split(",", 1)[-1])
    try:
        return base64.b64decode(cleaned, validate=False)
    except Exception:
        return None


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:80] or "package"
