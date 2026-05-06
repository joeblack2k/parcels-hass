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


def test_vinted_cross_reference_does_not_override_carrier_in_transit_with_pickup_destination():
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
    assert record["status"] == "in_transit"
    assert "pickup_location" not in record
    assert record["shop"] == "Vinted"
    assert record["source"] == "vinted_cross_reference"


def test_chronopost_in_transit_refresh_clears_stale_vinted_pickup_cross_reference():
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

    assert merged["status"] == "in_transit"
    assert merged["pickup_location"] is None
    assert merged["source"] == "vinted_cross_reference"
    assert merged["tracking_refresh_source"] == "ai_tracking_page"
    assert merged["tracking_refresh_has_delivery_detail"] is True


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
    assert record["status"] == "in_transit"
    assert "pickup_location" not in record
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


def test_vinted_sidecar_can_correct_stale_pickup_to_in_transit():
    record = apply_vinted_cross_reference(
        {
            "carrier": "vinted",
            "shop": "Vinted",
            "tracking_code": "XU152297803JF",
            "status": "in_transit",
            "source": "vinted_sidecar",
            "confidence": "high",
            "extra": {
                "carrier_tracking": {
                    "carrier": "chronopost",
                    "tracking_code": "XU152297803JF",
                }
            },
        },
        {
            "chronopost:xu152297803jf": {
                "carrier": "chronopost",
                "shop": "Vinted",
                "tracking_code": "XU152297803JF",
                "status": "ready_for_pickup",
                "pickup_location": "DROOMVISIE - SCHOOLSTRAAT 109A - VOORSCHOTEN",
                "pickup_code": "034049",
                "source": "vinted_cross_reference",
                "extra": {"vinted_cross_reference": {"status": "ready_for_pickup"}},
            }
        },
    )

    assert record["carrier"] == "chronopost"
    assert record["status"] == "in_transit"
    assert record["pickup_location"] is None
    assert record["pickup_code"] is None
    assert record["source"] == "vinted_sidecar_cross_reference"


def test_vinted_sidecar_pickup_keeps_location_and_uses_vinted_source():
    record = apply_vinted_cross_reference(
        {
            "carrier": "vinted",
            "shop": "Vinted",
            "tracking_code": "XU152297803JF",
            "status": "ready_for_pickup",
            "pickup_location": "DROOMVISIE Schoolstraat 109A Voorschoten",
            "pickup_code": "034049",
            "source": "vinted_sidecar",
            "confidence": "high",
            "extra": {
                "carrier_tracking": {
                    "carrier": "chronopost",
                    "tracking_code": "XU152297803JF",
                }
            },
        },
        {},
    )

    assert record["carrier"] == "chronopost"
    assert record["status"] == "ready_for_pickup"
    assert record["pickup_location"] == "DROOMVISIE Schoolstraat 109A Voorschoten"
    assert record["pickup_code"] == "034049"
    assert record["source"] == "vinted_sidecar_cross_reference"


def test_vinted_dpd_manual_link_merges_platform_and_carrier_numbers():
    record = apply_vinted_cross_reference(
        {
            "carrier": "vinted",
            "shop": "Vinted",
            "tracking_code": "1778051829299958",
            "status": "in_transit",
            "expected_date": "2026-05-11",
            "tracking_status_text": "5 artikelen - verwacht 2026-05-11 t/m 2026-05-13",
            "source": "vinted_sidecar_manual_link",
            "confidence": "high",
            "extra": {
                "vinted_id": "1778051829299958",
                "vinted_tracking_code": "1778051829299958",
                "vinted_item_title": "5 artikelen",
                "vinted_other_party": "bruijna1981",
                "expected_date_end": "2026-05-13",
                "carrier_tracking": {
                    "carrier": "dpd",
                    "tracking_code": "34343180322236",
                    "tracking_url": "https://www.dpd.com/nl/nl/ontvangen/volgen/?parcelNumber=34343180322236",
                },
                "tracking_events": [
                    {"status": "Onderweg", "timestamp": "2026-05-06T14:49:00+02:00"},
                ],
            },
        },
        {
            "dpd:34343180322236": {
                "key": "dpd:34343180322236",
                "carrier": "dpd",
                "shop": "Aissatou Drame",
                "tracking_code": "34343180322236",
                "status": "in_transit",
                "source": "imap_corrected",
                "confidence": "high",
            }
        },
    )

    assert record["key"] == "dpd:34343180322236"
    assert record["carrier"] == "dpd"
    assert record["tracking_code"] == "34343180322236"
    assert record["shop"] == "Aissatou Drame"
    assert record["expected_date"] == "2026-05-11"
    assert record["source"] == "vinted_sidecar_cross_reference"
    assert record["extra"]["vinted_tracking_code"] == "1778051829299958"
    assert record["extra"]["vinted_item_title"] == "5 artikelen"
    assert record["extra"]["expected_date_end"] == "2026-05-13"
    assert record["extra"]["tracking_events"][0]["status"] == "Onderweg"
