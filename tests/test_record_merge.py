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

from custom_components.package_inbox.record_merge import apply_vinted_cross_reference, merge_tracking_update


def test_vinted_cross_reference_promotes_record_to_carrier_key_shape():
    record = apply_vinted_cross_reference(
        {
            "carrier": "vinted",
            "shop": "Vinted",
            "tracking_code": "5ARTIKELENNO",
            "status": "ready_for_pickup",
            "pickup_location": "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN",
            "pickup_code": "034049",
            "source": "imap",
            "confidence": "high",
            "extra": {
                "carrier_tracking": {
                    "carrier": "chronopost",
                    "tracking_code": "XU152297803JF",
                    "tracking_url": "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
                }
            },
        },
        {},
    )

    assert record["carrier"] == "chronopost"
    assert record["shop"] == "Vinted"
    assert record["tracking_code"] == "XU152297803JF"
    assert record["tracking_url"].startswith("https://www.chronopost.fr/")
    assert record["status"] == "ready_for_pickup"
    assert record["pickup_location"] == "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN"
    assert record["source"] == "vinted_cross_reference"
    assert record["extra"]["vinted_cross_reference"]["status"] == "ready_for_pickup"


def test_vinted_cross_reference_overrides_existing_weaker_chronopost_record():
    existing = {
        "key": "chronopost:xu152297803jf",
        "carrier": "chronopost",
        "shop": "Chronopost",
        "tracking_code": "XU152297803JF",
        "status": "in_transit",
        "source": "tracking_correction_chronopost_latest_event",
        "confidence": "high",
    }
    vinted = {
        "key": "vinted:5artikelenno",
        "carrier": "vinted",
        "shop": "Vinted",
        "tracking_code": "5ARTIKELENNO",
        "status": "ready_for_pickup",
        "pickup_location": "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN",
        "source": "imap",
        "confidence": "high",
        "extra": {
            "carrier_tracking": {
                "carrier": "chronopost",
                "tracking_code": "XU152297803JF",
            }
        },
    }

    record = apply_vinted_cross_reference(vinted, {"chronopost:xu152297803jf": existing})

    assert record["carrier"] == "chronopost"
    assert record["status"] == "ready_for_pickup"
    assert record["pickup_location"] == "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN"
    assert record["shop"] == "Vinted"
    assert record["source"] == "vinted_cross_reference"


def test_weak_chronopost_refresh_preserves_vinted_pickup_state():
    merged = merge_tracking_update(
        {
            "carrier": "chronopost",
            "shop": "Vinted",
            "tracking_code": "XU152297803JF",
            "status": "ready_for_pickup",
            "pickup_location": "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN",
            "source": "vinted_cross_reference",
            "extra": {"vinted_cross_reference": {"status": "ready_for_pickup"}},
        },
        {
            "carrier": "chronopost",
            "tracking_code": "XU152297803JF",
            "status": "in_transit",
            "tracking_refresh_source": "ai_tracking_page",
            "tracking_refresh_url": "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
            "tracking_status_text": "Suivez votre colis Loading...",
        },
        "2026-05-06T14:00:14+02:00",
    )

    assert merged["status"] == "ready_for_pickup"
    assert merged["pickup_location"] == "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN"
    assert merged["source"] == "vinted_cross_reference"
    assert merged["tracking_refresh_source"] == "ai_tracking_page"
    assert merged["tracking_refresh_has_delivery_detail"] is False


def test_existing_carrier_record_with_vinted_reference_keeps_new_refresh_diagnostics():
    record = apply_vinted_cross_reference(
        {
            "carrier": "chronopost",
            "shop": "Vinted",
            "tracking_code": "XU152297803JF",
            "status": "ready_for_pickup",
            "tracking_refresh_source": "local_tracking_scraper",
            "tracking_refresh_url": "http://local-parcels-fedex-scraper:8765/track",
            "tracking_last_checked": "2026-05-06T15:00:00+02:00",
            "source": "vinted_cross_reference",
            "extra": {
                "carrier_tracking": {
                    "carrier": "chronopost",
                    "tracking_code": "XU152297803JF",
                },
                "vinted_cross_reference": {"status": "ready_for_pickup"},
            },
        },
        {
            "chronopost:xu152297803jf": {
                "carrier": "chronopost",
                "shop": "Vinted",
                "tracking_code": "XU152297803JF",
                "status": "ready_for_pickup",
                "tracking_refresh_source": "ai_tracking_page",
                "tracking_last_checked": "2026-05-06T14:00:00+02:00",
            }
        },
    )

    assert record["status"] == "ready_for_pickup"
    assert record["tracking_refresh_source"] == "local_tracking_scraper"
    assert record["tracking_refresh_url"] == "http://local-parcels-fedex-scraper:8765/track"
    assert record["tracking_last_checked"] == "2026-05-06T15:00:00+02:00"


def test_later_carrier_record_links_back_to_stored_vinted_reference():
    record = apply_vinted_cross_reference(
        {
            "carrier": "chronopost",
            "shop": "Chronopost",
            "tracking_code": "XU152297803JF",
            "status": "in_transit",
            "source": "imap",
        },
        {
            "vinted:5artikelenno": {
                "key": "vinted:5artikelenno",
                "carrier": "vinted",
                "shop": "Vinted",
                "tracking_code": "5ARTIKELENNO",
                "status": "ready_for_pickup",
                "pickup_location": "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN",
                "source": "imap",
                "confidence": "high",
                "extra": {
                    "carrier_tracking": {
                        "carrier": "chronopost",
                        "tracking_code": "XU152297803JF",
                    }
                },
            }
        },
    )

    assert record["carrier"] == "chronopost"
    assert record["status"] == "ready_for_pickup"
    assert record["pickup_location"] == "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN"
    assert record["extra"]["vinted_cross_reference"]["key"] == "vinted:5artikelenno"


def test_strong_chronopost_delivery_update_can_still_win():
    merged = merge_tracking_update(
        {
            "carrier": "chronopost",
            "tracking_code": "XU152297803JF",
            "status": "ready_for_pickup",
            "pickup_location": "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN",
            "source": "vinted_cross_reference",
            "extra": {"vinted_cross_reference": {"status": "ready_for_pickup"}},
        },
        {
            "carrier": "chronopost",
            "tracking_code": "XU152297803JF",
            "status": "delivered",
            "tracking_refresh_source": "local_tracking_scraper",
            "tracking_status_text": "Livré",
        },
        "2026-05-06T15:00:00+02:00",
    )

    assert merged["status"] == "delivered"
    assert merged["pickup_location"] is None
    assert merged["tracking_status_text"] == "Livré"
