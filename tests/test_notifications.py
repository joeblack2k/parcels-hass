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
    split_pickup_location,
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
