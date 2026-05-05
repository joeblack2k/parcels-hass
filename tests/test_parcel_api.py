from pathlib import Path
import sys
import types

REPO_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_DIR / "custom_components" / "package_inbox"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(REPO_DIR / "custom_components")]
package_inbox = types.ModuleType("custom_components.package_inbox")
package_inbox.__path__ = [str(PACKAGE_DIR)]
sys.modules.setdefault("custom_components", custom_components)
sys.modules.setdefault("custom_components.package_inbox", package_inbox)

from custom_components.package_inbox.parcel_api import (
    PARCEL_API_STATUS_CODES,
    map_parcel_api_status_code,
    parcel_api_status_from_code,
)


def test_parcel_api_status_code_mapping_labels_and_internal_statuses():
    expected = {
        0: ("completed", "delivered"),
        1: ("frozen", "unknown"),
        2: ("in_transit", "in_transit"),
        3: ("pickup", "ready_for_pickup"),
        4: ("out_for_delivery", "expected_today"),
        5: ("not_found", "unknown"),
        6: ("failed", "unknown"),
        7: ("exception", "unknown"),
        8: ("info_received", "in_transit"),
    }

    assert set(PARCEL_API_STATUS_CODES) == set(expected)
    for code, (label, internal_status) in expected.items():
        mapped = parcel_api_status_from_code(code)
        assert mapped is not None
        assert mapped.parcel_label == label
        assert mapped.status == internal_status
        assert map_parcel_api_status_code(str(code)) == internal_status


def test_unknown_parcel_api_status_code_maps_unknown():
    assert parcel_api_status_from_code("bogus") is None
    assert map_parcel_api_status_code(999) == "unknown"
