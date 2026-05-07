from datetime import datetime, timezone
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

from custom_components.package_inbox.deleted_imports import (
    deleted_import_tombstone_for_record,
    prune_deleted_imports,
    record_matches_deleted_import,
)


def test_deleted_vinted_record_creates_tombstone_that_blocks_reimport():
    record = {
        "key": "vinted:15633228733",
        "carrier": "vinted",
        "shop": "Vinted",
        "tracking_code": "15633228733",
        "source": "vinted_sidecar",
        "extra": {
            "vinted_id": "15633228733",
            "vinted_thread_id": "18223968074",
            "vinted_source_url": "https://www.vinted.nl/inbox/18223968074",
            "vinted_item_title": "Lilo en stitch 2",
        },
    }

    tombstone = deleted_import_tombstone_for_record(
        "vinted:15633228733",
        record,
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )

    assert tombstone is not None
    assert "vinted:15633228733" in tombstone["identifiers"]
    assert "18223968074" in tombstone["identifiers"]
    assert record_matches_deleted_import(record, {"vinted": [tombstone]})
    assert record_matches_deleted_import(
        {
            "carrier": "vinted",
            "shop": "Vinted",
            "tracking_code": "15633228733",
            "source": "vinted_sidecar",
            "extra": {"vinted_thread_id": "18223968074"},
        },
        {"vinted": [tombstone]},
    )


def test_deleted_import_tombstone_does_not_block_unrelated_vinted_order():
    tombstone = deleted_import_tombstone_for_record(
        "vinted:15633228733",
        {
            "carrier": "vinted",
            "tracking_code": "15633228733",
            "source": "vinted_sidecar",
            "extra": {"vinted_thread_id": "18223968074"},
        },
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )

    assert not record_matches_deleted_import(
        {
            "carrier": "vinted",
            "tracking_code": "19548096183",
            "source": "vinted_sidecar",
            "extra": {"vinted_thread_id": "22283644668"},
        },
        {"vinted": [tombstone]},
    )


def test_prune_deleted_imports_drops_expired_entries():
    deleted_imports = {
        "vinted": [
            {"deleted_at": "2025-01-01T00:00:00+00:00", "identifiers": ["old"]},
            {"deleted_at": "2026-05-01T00:00:00+00:00", "identifiers": ["current"]},
        ]
    }

    prune_deleted_imports(deleted_imports, now=datetime(2026, 5, 7, tzinfo=timezone.utc), retention_days=30)

    assert deleted_imports == {"vinted": [{"deleted_at": "2026-05-01T00:00:00+00:00", "identifiers": ["current"]}]}
