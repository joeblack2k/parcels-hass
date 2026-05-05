"""Constants for Parcels for Home Assistant."""

from __future__ import annotations

DOMAIN = "package_inbox"

CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_ENABLE_AI_CLASSIFICATION = "enable_ai_classification"
CONF_ENABLE_AI_FALLBACK = "enable_ai_fallback"
CONF_ENABLE_EVENT_LISTENER = "enable_event_listener"
CONF_ENABLE_TRACKING_REFRESH = "enable_tracking_refresh"
CONF_DELIVERY_HOUSE_NUMBER = "delivery_house_number"
CONF_DELIVERY_POSTCODE = "delivery_postcode"
CONF_IMAP_ENTRY_ID = "imap_entry_id"
CONF_MATRIX_ROOM_ID = "matrix_room_id"
CONF_NOTIFY_SCRIPT = "notify_script"
CONF_POSTNL_DELIVERY_SENSOR = "postnl_delivery_sensor"
CONF_PUBLIC_QR_DIR = "public_qr_dir"
CONF_TRACKING_REFRESH_MINUTES = "tracking_refresh_minutes"
CONF_TRACKING_SCRAPER_TOKEN = "tracking_scraper_token"
CONF_TRACKING_SCRAPER_URL = "tracking_scraper_url"
CONF_TRACKING_TIMEOUT = "tracking_timeout"
CONF_TRACKING_USER_AGENT = "tracking_user_agent"

DEFAULT_AI_TASK_ENTITY = "ai_task.google_ai_task"
DEFAULT_DELIVERY_HOUSE_NUMBER = ""
DEFAULT_DELIVERY_POSTCODE = ""
DEFAULT_MATRIX_ROOM_ID = ""
DEFAULT_NOTIFY_SCRIPT = "persistent_notification.create"
DEFAULT_POSTNL_DELIVERY_SENSOR = ""
DEFAULT_PUBLIC_QR_DIR = "package_inbox"
DEFAULT_TRACKING_REFRESH_MINUTES = 60
DEFAULT_TRACKING_SCRAPER_TOKEN = ""
DEFAULT_TRACKING_SCRAPER_URL = ""
DEFAULT_TRACKING_TIMEOUT = 15
DEFAULT_TRACKING_USER_AGENT = "Parcels for Home Assistant/0.2 (+https://github.com/joeblack2k/parcels-hass)"

SERVICE_ADD_PACKAGE = "add_package"
SERVICE_DEBUG_PARSE = "debug_parse"
SERVICE_DELETE_PACKAGE = "delete_package"
SERVICE_MARK_PICKED_UP = "mark_picked_up"
SERVICE_PROCESS_IMAP_EVENT = "process_imap_event"
SERVICE_REFRESH_TRACKING = "refresh_tracking"
SERVICE_SEND_MORNING_SUMMARY = "send_morning_summary"
SERVICE_SEND_PICKUP_SUMMARY = "send_pickup_summary"
SERVICE_SET_STATUS = "set_status"

STATUS_DELIVERED = "delivered"
STATUS_EXPECTED_TODAY = "expected_today"
STATUS_IN_TRANSIT = "in_transit"
STATUS_PICKED_UP = "picked_up"
STATUS_READY_FOR_PICKUP = "ready_for_pickup"
STATUS_UNKNOWN = "unknown"

STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1
