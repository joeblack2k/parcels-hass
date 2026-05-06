"""Parcels sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PackageInboxEntity


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    """Set up Parcels sensors."""
    if DOMAIN not in hass.data:
        return

    async_add_entities(
        [
            PackageInboxDashboardSensor(hass),
            PackageInboxExpectedTodaySensor(hass),
            PackageInboxPickupReadySensor(hass),
            PackageInboxNextDeliveryWindowSensor(hass),
            PackageInboxDeliveryWindowWeightSensor(hass),
        ]
    )


class PackageInboxDashboardSensor(PackageInboxEntity, SensorEntity):
    """Dashboard payload for all active and recent packages."""

    _attr_icon = "mdi:package-variant-closed"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            key="dashboard",
            name="Parcels Dashboard",
        )

    @callback
    def _refresh(self) -> None:
        self._snapshot = self.manager.dashboard_snapshot()
        self._update_from_snapshot()

    @callback
    def _update_from_snapshot(self) -> None:
        counts = self._snapshot.get("counts") or {}
        self._attr_native_value = counts.get("active", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._snapshot)


class PackageInboxExpectedTodaySensor(PackageInboxEntity, SensorEntity):
    """Number of delivery packages expected today."""

    _attr_icon = "mdi:package-variant-closed"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            key="expected_today",
            name="Parcels Expected Today",
        )

    @callback
    def _update_from_snapshot(self) -> None:
        self._attr_native_value = self._snapshot.get("expected_today_count", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["packages"] = self._snapshot.get("packages") or []
        return attrs


class PackageInboxPickupReadySensor(PackageInboxEntity, SensorEntity):
    """Number of packages waiting at pickup points."""

    _attr_icon = "mdi:package-check"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            key="pickup_ready",
            name="Parcels Pickup Ready",
        )

    @callback
    def _refresh(self) -> None:
        self._snapshot = self.manager.dashboard_snapshot()
        self._update_from_snapshot()

    @callback
    def _update_from_snapshot(self) -> None:
        counts = self._snapshot.get("counts") or {}
        self._attr_native_value = counts.get("pickup", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["packages"] = [
            record
            for record in self._snapshot.get("active") or []
            if isinstance(record, dict) and record.get("status") == "ready_for_pickup"
        ]
        return attrs


class PackageInboxNextDeliveryWindowSensor(PackageInboxEntity, SensorEntity):
    """Next known package delivery window."""

    _attr_icon = "mdi:truck-clock"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            key="next_delivery_window",
            name="Parcels Next Delivery Window",
        )

    @callback
    def _update_from_snapshot(self) -> None:
        next_window = self._snapshot.get("next_window") or {}
        self._attr_native_value = next_window.get("window_start") or "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        next_window = self._snapshot.get("next_window") or {}
        attrs.update(
            {
                "window_start": next_window.get("window_start"),
                "window_end": next_window.get("window_end"),
                "carrier": next_window.get("carrier"),
                "shop": next_window.get("shop"),
                "tracking_code": next_window.get("tracking_code"),
            }
        )
        return attrs


class PackageInboxDeliveryWindowWeightSensor(PackageInboxEntity, SensorEntity):
    """Numeric package-delivery context weight for automations."""

    _attr_icon = "mdi:scale-balance"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            key="delivery_window_weight",
            name="Parcels Delivery Window Weight",
        )

    @callback
    def _update_from_snapshot(self) -> None:
        self._attr_native_value = self._snapshot.get("weight", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return super().extra_state_attributes
