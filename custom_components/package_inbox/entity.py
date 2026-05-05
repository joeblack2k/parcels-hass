"""Shared Parcels entity helpers."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .window import DEFAULT_WINDOW_MARGIN_MINUTES


class PackageInboxEntity(Entity):
    """Base entity backed by the Parcels manager."""

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, *, key: str, name: str) -> None:
        self.hass = hass
        self.manager = hass.data[DOMAIN]
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_has_entity_name = False
        self._snapshot: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Register update listeners."""
        self._refresh()
        self.async_on_remove(
            self.hass.bus.async_listen("package_inbox_updated", self._handle_package_update)
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, self._handle_time_update, timedelta(minutes=1))
        )

    @callback
    def _handle_package_update(self, event: Event) -> None:
        self._refresh()
        self.async_write_ha_state()

    @callback
    def _handle_time_update(self, now) -> None:
        self._refresh()
        self.async_write_ha_state()

    @callback
    def _refresh(self) -> None:
        self._snapshot = self.manager.delivery_snapshot(
            margin_minutes=DEFAULT_WINDOW_MARGIN_MINUTES,
        )
        self._update_from_snapshot()

    @callback
    def _update_from_snapshot(self) -> None:
        """Update entity state from the current snapshot."""
        raise NotImplementedError

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return common diagnostic attributes."""
        return {
            "reason": self._snapshot.get("reason"),
            "margin_minutes": self._snapshot.get("margin_minutes"),
            "expected_today_count": self._snapshot.get("expected_today_count"),
            "window_count": self._snapshot.get("window_count"),
            "active_packages": self._snapshot.get("active_packages") or [],
            "next_window": self._snapshot.get("next_window"),
            "windows": self._snapshot.get("windows") or [],
        }
