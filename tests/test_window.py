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

from custom_components.package_inbox.window import build_delivery_snapshot


TZ = ZoneInfo("Europe/Amsterdam")


def test_delivery_snapshot_inside_window_has_weight_3():
    snapshot = build_delivery_snapshot(
        [
            {
                "key": "dhl:1",
                "carrier": "dhl",
                "shop": "Coolblue",
                "status": "expected_today",
                "expected_date": "2026-04-29",
                "delivery_window_start": "11:45",
                "delivery_window_end": "12:45",
            }
        ],
        now=datetime(2026, 4, 29, 12, 0, tzinfo=TZ),
    )

    assert snapshot["active"] is True
    assert snapshot["weight"] == 3
    assert snapshot["reason"] == "inside_delivery_window"
    assert snapshot["active_packages"][0]["key"] == "dhl:1"


def test_delivery_snapshot_near_window_has_weight_2():
    snapshot = build_delivery_snapshot(
        [
            {
                "key": "dhl:1",
                "carrier": "dhl",
                "shop": "Coolblue",
                "status": "expected_today",
                "expected_date": "2026-04-29",
                "delivery_window_start": "11:45",
                "delivery_window_end": "12:45",
            }
        ],
        now=datetime(2026, 4, 29, 11, 20, tzinfo=TZ),
    )

    assert snapshot["active"] is True
    assert snapshot["weight"] == 2
    assert snapshot["reason"] == "near_delivery_window"


def test_delivery_snapshot_ignores_pickup_and_picked_up():
    snapshot = build_delivery_snapshot(
        [
            {
                "key": "vinted:1",
                "carrier": "vinted",
                "status": "picked_up",
                "expected_date": "2026-04-29",
                "delivery_window_start": "11:45",
                "delivery_window_end": "12:45",
            },
            {
                "key": "apotheek:1",
                "carrier": "apotheek",
                "status": "ready_for_pickup",
            },
        ],
        now=datetime(2026, 4, 29, 12, 0, tzinfo=TZ),
    )

    assert snapshot["active"] is False
    assert snapshot["weight"] == 0
    assert snapshot["expected_today_count"] == 0


def test_delivery_snapshot_dedupes_untracked_summary_mails():
    snapshot = build_delivery_snapshot(
        [
            {
                "key": "imap:1",
                "carrier": "amazon",
                "shop": "Amazon",
                "status": "in_transit",
                "expected_date": "2026-04-29",
            },
            {
                "key": "imap:2",
                "carrier": "amazon",
                "shop": "Amazon",
                "status": "in_transit",
                "expected_date": "2026-04-29",
            },
            {
                "key": "dhl:1",
                "carrier": "dhl",
                "shop": "Amazon",
                "status": "expected_today",
                "expected_date": "2026-04-29",
                "delivery_window_start": "17:30",
                "delivery_window_end": "21:30",
            },
        ],
        now=datetime(2026, 4, 29, 10, 0, tzinfo=TZ),
    )

    assert snapshot["expected_today_count"] == 2


def test_delivery_snapshot_dedupes_tracked_record_and_live_integration_same_window():
    snapshot = build_delivery_snapshot(
        [
            {
                "key": "postnl:3sbvms6743345",
                "carrier": "postnl",
                "shop": "De Lijsten Fabriek",
                "tracking_code": "3SBVMS6743345",
                "status": "expected_today",
                "expected_date": "2026-05-01",
                "delivery_window_start": "10:20",
                "delivery_window_end": "12:20",
            },
            {
                "key": "digest:integration",
                "carrier": "postnl",
                "shop": "De Lijsten Fabriek",
                "status": "expected_today",
                "expected_date": "2026-05-01",
                "delivery_window_start": "10:20",
                "delivery_window_end": "12:20",
                "source": "postnl_integration",
            },
        ],
        now=datetime(2026, 5, 1, 9, 0, tzinfo=TZ),
    )

    assert snapshot["expected_today_count"] == 1
    assert snapshot["packages"][0]["tracking_code"] == "3SBVMS6743345"


def test_delivery_snapshot_ignores_zero_length_placeholder_window():
    snapshot = build_delivery_snapshot(
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
        now=datetime(2026, 5, 5, 0, 5, tzinfo=TZ),
    )

    assert snapshot["weight"] == 1
    assert snapshot["active_packages"] == []
    assert snapshot["windows"] == []
    assert snapshot["packages"][0]["delivery_window_start"] is None
    assert snapshot["packages"][0]["delivery_window_end"] is None
