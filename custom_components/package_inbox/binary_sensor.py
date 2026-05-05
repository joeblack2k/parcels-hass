"""Parcels binary sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
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
    """Set up Parcels binary sensors."""
    if DOMAIN not in hass.data:
        return

    async_add_entities([PackageInboxDeliveryWindowActiveBinarySensor(hass)])


class PackageInboxDeliveryWindowActiveBinarySensor(PackageInboxEntity, BinarySensorEntity):
    """Whether a package delivery window is active or near-active."""

    _attr_icon = "mdi:truck-delivery"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            key="delivery_window_active",
            name="Parcels Delivery Window Active",
        )

    @callback
    def _update_from_snapshot(self) -> None:
        self._attr_is_on = bool(self._snapshot.get("active"))
