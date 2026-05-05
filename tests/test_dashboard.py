from datetime import datetime
from pathlib import Path
import sys
import types
from zoneinfo import ZoneInfo

REPO_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_DIR / "custom_components" / "package_inbox"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(REPO_DIR / "custom_components")]
package_inbox = types.ModuleType("custom_components.package_inbox")
package_inbox.__path__ = [str(PACKAGE_DIR)]
sys.modules.setdefault("custom_components", custom_components)
sys.modules.setdefault("custom_components.package_inbox", package_inbox)

from custom_components.package_inbox.dashboard import build_dashboard_snapshot


TZ = ZoneInfo("Europe/Amsterdam")


def test_dashboard_active_history_counts_and_window_weight():
    snapshot = build_dashboard_snapshot(
        [
            {
                "key": "fedex:871354982751",
                "carrier": "fedex",
                "shop": "Ubiquiti",
                "tracking_code": "871354982751",
                "status": "in_transit",
                "expected_date": "2026-05-05",
            },
            {
                "key": "vinted:old",
                "carrier": "vinted",
                "shop": "Vinted Go",
                "status": "picked_up",
                "updated_at": "2026-05-04T12:00:00+02:00",
            },
        ],
        delivery_snapshot={"weight": 2, "active_packages": [{"key": "dhl:1"}]},
        now=datetime(2026, 5, 4, 22, 0, tzinfo=TZ),
    )

    assert snapshot["counts"]["active"] == 1
    assert snapshot["counts"]["history"] == 1
    assert snapshot["counts"]["in_delivery_window"] == 1
    assert snapshot["delivery_window_weight"] == 2
    assert snapshot["active"][0]["key"] == "fedex:871354982751"
    assert snapshot["history"][0]["key"] == "vinted:old"


def test_dashboard_stale_past_delivery_leaves_active_list():
    snapshot = build_dashboard_snapshot(
        [
            {
                "key": "amazon:stale",
                "carrier": "amazon",
                "shop": "Amazon",
                "tracking_code": "TBA123",
                "status": "in_transit",
                "expected_date": "2026-04-29",
                "updated_at": "2026-04-29T09:00:00+02:00",
            }
        ],
        now=datetime(2026, 5, 4, 22, 0, tzinfo=TZ),
    )

    assert snapshot["counts"]["active"] == 0
    assert snapshot["counts"]["history"] == 1
    assert snapshot["history"][0]["key"] == "amazon:stale"


def test_dashboard_keeps_unknown_tracking_active_without_date():
    snapshot = build_dashboard_snapshot(
        [
            {
                "key": "dhl:manual",
                "carrier": "dhl",
                "shop": "Handmatig",
                "tracking_code": "JJD000090254000059755497",
                "status": "unknown",
            }
        ],
        now=datetime(2026, 5, 4, 22, 0, tzinfo=TZ),
    )

    assert snapshot["counts"]["active"] == 1
    assert snapshot["active"][0]["carrier_title"] == "DHL"


def test_dashboard_unknown_pickup_text_without_ready_status_is_history():
    snapshot = build_dashboard_snapshot(
        [
            {
                "key": "pickup:bad-click",
                "carrier": "postnl",
                "shop": "Amazon",
                "pickup_location": "pakketpunt",
                "pickup_code": "KLIK",
                "status": "unknown",
            }
        ],
        now=datetime(2026, 5, 4, 22, 0, tzinfo=TZ),
    )

    assert snapshot["counts"]["active"] == 0
    assert snapshot["history"][0]["key"] == "pickup:bad-click"


def test_dashboard_hides_zero_length_placeholder_window():
    snapshot = build_dashboard_snapshot(
        [
            {
                "key": "fedex:1",
                "carrier": "fedex",
                "shop": "Ubiquiti",
                "tracking_code": "871354982751",
                "status": "in_transit",
                "expected_date": "2026-05-05",
                "delivery_window_start": "00:00",
                "delivery_window_end": "00:00",
            }
        ],
        now=datetime(2026, 5, 5, 0, 15, tzinfo=TZ),
    )

    record = snapshot["active"][0]
    assert record["delivery_window_start"] is None
    assert record["delivery_window_end"] is None
    assert record["display_subtitle"] == "FedEx 2026-05-05"
