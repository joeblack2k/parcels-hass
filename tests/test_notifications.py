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

from custom_components.package_inbox.notifications import (
    format_pickup_notification,
    format_pickup_summary,
    format_vinted_tracking_notification,
    should_notify_vinted_tracking,
    split_pickup_location,
    vinted_tracking_fingerprint,
)


def test_vinted_pickup_notification_uses_vinted_detail_template():
    message = format_pickup_notification(
        {
            "carrier": "vinted",
            "shop": "Vinted",
            "status": "ready_for_pickup",
            "pickup_location": "DROOMVISIE Schoolstraat 109A Voorschoten",
            "pickup_code": "034049",
            "extra": {
                "vinted_item_title": "Vest 128",
                "pickup_deadline": "2026-05-13",
            },
        }
    )

    assert message == (
        "Vinted pakket ligt klaar!\n"
        "Artikel: Vest 128\n"
        "Winkel: DROOMVISIE\n"
        "Adres: Schoolstraat 109A Voorschoten\n"
        "Code: 034049\n"
        "Ophalen tot: 2026-05-13"
    )


def test_vinted_pickup_notification_keeps_missing_fields_explicit():
    message = format_pickup_notification(
        {
            "carrier": "vinted",
            "shop": "Vinted",
            "status": "ready_for_pickup",
            "extra": {"vinted_item_title": "Vest 128"},
        }
    )

    assert message == (
        "Vinted pakket ligt klaar!\n"
        "Artikel: Vest 128\n"
        "Winkel: onbekend\n"
        "Adres: onbekend\n"
        "Code: onbekend\n"
        "Ophalen tot: onbekend"
    )


def test_generic_pickup_notification_stays_compact():
    assert (
        format_pickup_notification(
            {
                "carrier": "dhl",
                "shop": "Bol.com",
                "status": "ready_for_pickup",
                "pickup_location": "DHL ServicePoint",
                "pickup_code": "ABCD12",
            }
        )
        == "Bol.com pakket ligt klaar bij DHL ServicePoint\n\nVervoerder: DHL\nCode: ABCD12"
    )


def test_vinted_pickup_summary_includes_actionable_fields():
    message = format_pickup_summary(
        [
            {
                "carrier": "vinted",
                "shop": "Vinted",
                "status": "ready_for_pickup",
                "pickup_location": "HEMA LEIDSCHENDAM, DAMLAAN 44, 2265 AP LEIDSCHENDAM",
                "pickup_code": "1480",
                "extra": {
                    "vinted_item_title": "Vest 128",
                    "pickup_deadline": "2026-05-13",
                },
            }
        ]
    )

    assert "- Vinted: Vest 128" in message
    assert "  Winkel: HEMA LEIDSCHENDAM" in message
    assert "  Adres: DAMLAAN 44, 2265 AP LEIDSCHENDAM" in message
    assert "  Code: 1480" in message
    assert "  Ophalen tot: 2026-05-13" in message


def test_split_pickup_location_handles_common_vinted_shapes():
    assert split_pickup_location("DROOMVISIE Schoolstraat 109A Voorschoten") == (
        "DROOMVISIE",
        "Schoolstraat 109A Voorschoten",
    )
    assert split_pickup_location("HEMA LEIDSCHENDAM, DAMLAAN 44, 2265 AP LEIDSCHENDAM") == (
        "HEMA LEIDSCHENDAM",
        "DAMLAAN 44, 2265 AP LEIDSCHENDAM",
    )


def test_vinted_tracking_notification_includes_app_visible_tracking_card():
    record = {
        "carrier": "chronopost",
        "shop": "Vinted",
        "status": "in_transit",
        "tracking_code": "XU152297803JF",
        "expected_date": "2026-05-06",
        "tracking_url": "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF&langue=nl",
        "extra": {
            "vinted_item_title": "5 artikelen",
            "vinted_other_party": "bruijna1981",
            "expected_date_end": "2026-05-08",
            "carrier_tracking": {
                "carrier": "chronopost",
                "tracking_code": "XU152297803JF",
                "tracking_url": "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF&langue=nl",
            },
            "tracking_events": [
                {"status": "Onderweg", "timestamp": "2026-05-06T21:09:00+02:00"},
                {"status": "Aangekomen bij sorteercentrum", "timestamp": "2026-05-05T17:55:00+02:00"},
            ],
        },
    }

    assert should_notify_vinted_tracking(record)
    assert format_vinted_tracking_notification(record) == (
        "Vinted pakket is onderweg\n"
        "Artikel: 5 artikelen\n"
        "Vinted: bruijna1981\n"
        "Vervoerder: Chronopost\n"
        "Trackingcode: XU152297803JF\n"
        "Verwacht: 6 mei - 8 mei\n"
        "Tracking: https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF&langue=nl\n"
        "\n"
        "Trackinginformatie:\n"
        "- Onderweg: 6 mei 2026 21:09\n"
        "- Aangekomen bij sorteercentrum: 5 mei 2026 17:55"
    )


def test_vinted_tracking_notification_uses_cross_reference_eta_when_tracking_refresh_lacks_eta():
    record = {
        "carrier": "chronopost",
        "shop": "Vinted",
        "status": "in_transit",
        "tracking_code": "XU152297803JF",
        "tracking_url": "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
        "extra": {
            "vinted_item_title": "5 artikelen",
            "expected_date_end": "2026-05-13",
            "vinted_cross_reference": {
                "expected_date": "2026-05-11",
                "vinted_expected_date_to": "2026-05-13",
                "tracking_events": [{"status": "Onderweg", "timestamp": "2026-05-06T14:49:00+02:00"}],
            },
        },
    }

    message = format_vinted_tracking_notification(record)

    assert "Verwacht: 11 mei - 13 mei" in message
    assert "- Onderweg: 6 mei 2026 14:49" in message


def test_vinted_tracking_fingerprint_changes_on_latest_event():
    base = {
        "carrier": "vinted",
        "shop": "Vinted",
        "status": "in_transit",
        "tracking_code": "1778051829299958",
        "expected_date": "2026-05-11",
        "extra": {
            "vinted_item_title": "5 artikelen",
            "tracking_events": [{"status": "Verzonden", "timestamp": "2026-05-06T10:10:00+02:00"}],
        },
    }
    changed = {
        **base,
        "extra": {
            **base["extra"],
            "tracking_events": [{"status": "Onderweg", "timestamp": "2026-05-06T14:49:00+02:00"}],
        },
    }

    assert vinted_tracking_fingerprint(base) != vinted_tracking_fingerprint(changed)
