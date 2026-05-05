from datetime import date
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

from custom_components.package_inbox.tracking import (
    build_fedex_tracking_api_url,
    build_fedex_tracking_payload,
    build_tracking_api_url,
    build_tracking_url,
    extract_fedex_tracking_update_from_mail,
    extract_fedex_tracking_update_from_json,
    extract_tracking_update,
    extract_tracking_update_from_json,
    normalize_tracking_scraper_update,
)


TODAY = date(2026, 4, 25)


def test_builds_dhl_tracking_url():
    assert (
        build_tracking_url("dhl", "JD0146001234567890")
        == "https://www.dhl.com/nl-nl/home/tracking.html?tracking-id=JD0146001234567890"
    )


def test_builds_postnl_tracking_url():
    assert (
        build_tracking_url("tntp", "3SBVMS6743345")
        == "https://www.postnl.nl/tracktrace/?B=3SBVMS6743345"
    )
    assert (
        build_tracking_url("tntp", "3SBVMS6743345", delivery_postcode="1234 AB")
        == "https://www.postnl.nl/tracktrace/?B=3SBVMS6743345&P=1234AB&D=NL"
    )


def test_builds_dhl_ecommerce_tracking_url_and_api_url():
    assert (
        build_tracking_url("dhl", "JJD000090254000059755497")
        == "https://my.dhlecommerce.nl/go-track-trace?role=consumer-receiver&tc=JJD000090254000059755497"
    )
    assert (
        build_tracking_api_url("dhl", "JJD000090254000059755497")
        == "https://my.dhlecommerce.nl/receiver-parcel-api/track-trace?key=JJD000090254000059755497&role=consumer-receiver"
    )
    assert (
        build_tracking_api_url("dhl", "JJD000090254000059755497", delivery_postcode="1234 AB")
        == "https://my.dhlecommerce.nl/receiver-parcel-api/track-trace?key=JJD000090254000059755497%2B1234AB&role=consumer-receiver"
    )


def test_builds_dhl_parcel_public_api_url_for_non_jjd_codes():
    assert (
        build_tracking_api_url("dhlnl", "3SBVMS6743345")
        == "https://api-gw.dhlparcel.nl/track-trace?key=3SBVMS6743345"
    )
    assert (
        build_tracking_api_url("dhlnl", "3SBVMS6743345", delivery_postcode="1234 AB")
        == "https://api-gw.dhlparcel.nl/track-trace?key=3SBVMS6743345%2B1234AB"
    )


def test_builds_fedex_tracking_url():
    assert (
        build_tracking_url("fedex", "871354982751")
        == "https://www.fedex.com/fedextrack/?trknbr=871354982751"
    )
    assert build_fedex_tracking_api_url() == "https://www.fedex.com/track/v2/shipments"
    assert build_fedex_tracking_payload("871354982751")["trackingInfo"][0]["trackNumberInfo"][
        "trackingNumber"
    ] == "871354982751"


def test_builds_chronopost_tracking_url_from_parcel_app_code():
    assert (
        build_tracking_url("chrono", "XU152297803JF")
        == "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF"
    )


def test_builds_postcode_aware_urls_for_nl_carriers():
    assert (
        build_tracking_url("trnkrpcode", "400123456", delivery_postcode="1234 AB", delivery_house_number="12")
        == "https://parcel.trunkrs.nl/400123456/1234AB"
    )
    assert (
        build_tracking_url("dynalogic", "ABC123", delivery_postcode="1234 AB", delivery_house_number="12")
        == "https://track.dynalogic.eu/?tracking=ABC123&postalCode=1234AB&houseNumber=12"
    )
    assert build_tracking_url("ups", "1Z999AA10123456784") == "https://www.ups.com/track?tracknum=1Z999AA10123456784"


def test_extracts_today_delivery_window_from_html():
    update = extract_tracking_update(
        carrier="dhl",
        tracking_code="JD0146001234567890",
        html="<html><body>Je DHL pakket wordt vandaag bezorgd tussen 15:00 en 17:00.</body></html>",
        fetched_url="https://example.test/track",
        today=TODAY,
    )

    assert update["status"] == "expected_today"
    assert update["expected_date"] == "2026-04-25"
    assert update["delivery_window_start"] == "15:00"
    assert update["delivery_window_end"] == "17:00"
    assert update["tracking_url"] == "https://example.test/track"


def test_marks_human_or_postcode_pages_soft_error():
    update = extract_tracking_update(
        carrier="dpd",
        tracking_code="05201234567890",
        html="<main>Vul je postcode in om je pakket te bekijken.</main>",
        today=TODAY,
    )

    assert update["tracking_refresh_error"] == "tracking_page_needs_human_or_postcode"
    assert update["tracking_refresh_supported"] is True
    assert update["tracking_status_text"] == ""


def test_marks_fedex_permission_page_as_blocked_without_status_text():
    update = extract_tracking_update(
        carrier="fedex",
        tracking_code="871354982751",
        html=(
            "<html><title>FedEx | System Down</title>"
            "<p>We're sorry, we can't process your request right now. "
            "It appears you don't have permission to view this webpage.</p></html>"
        ),
        today=TODAY,
    )

    assert update["tracking_refresh_error"] == "tracking_page_blocked_or_permission"
    assert update["tracking_status_text"] == ""
    assert update["status"] == "unknown"


def test_extracts_gls_public_tracking_delivered():
    update = extract_tracking_update(
        carrier="gls",
        tracking_code="12345678901",
        html="<main>GLS Netherlands: Het pakket is afgeleverd om 11:27.</main>",
        fetched_url="https://www.gls-info.nl/Tracking?match=12345678901",
        today=TODAY,
    )

    assert update["status"] == "delivered"
    assert update["tracking_refresh_source"] == "public_tracking_page"
    assert update["tracking_refresh_url"].startswith("https://www.gls-info.nl/Tracking")


def test_extracts_gls_public_tracking_in_delivery():
    update = extract_tracking_update(
        carrier="gls",
        tracking_code="12345678901",
        html="<main>Uw GLS pakket is onderweg naar het afleveradres en wordt vandaag bezorgd tussen 10:00 en 12:00.</main>",
        today=TODAY,
    )

    assert update["status"] == "expected_today"
    assert update["expected_date"] == "2026-04-25"
    assert update["delivery_window_start"] == "10:00"
    assert update["delivery_window_end"] == "12:00"


def test_future_delivered_text_does_not_mark_delivered():
    update = extract_tracking_update(
        carrier="gls",
        tracking_code="12345678901",
        html="<main>Your parcel will be delivered today between 10:00 and 12:00.</main>",
        today=TODAY,
    )

    assert update["status"] == "expected_today"


def test_extracts_dhl_delivered_from_public_json():
    update = extract_tracking_update_from_json(
        carrier="dhl",
        tracking_code="JJD000090254000059755497",
        payload=[
            {
                "barcode": "JJD000090254000059755497",
                "view": {
                    "message": "DELIVERED_AT",
                    "moment": "2026-04-29T17:06:42+02:00",
                    "phaseDisplay": [
                        {"phase": "DATA_RECEIVED", "completed": True},
                        {"phase": "UNDERWAY", "completed": True},
                        {"phase": "IN_DELIVERY", "completed": True},
                        {"phase": "DELIVERED", "completed": True},
                    ],
                    "deliveryMomentView": {
                        "stateMessage": "DELIVERED_AT",
                        "deliveredAt": "2026-04-29T17:06:42+02:00",
                    },
                },
                "deliveredAt": "2026-04-29T17:06:42+02:00",
            }
        ],
        today=date(2026, 5, 1),
    )

    assert update["status"] == "delivered"
    assert update["tracking_refresh_source"] == "public_tracking_api"
    assert "2026-04-29 17:06" in update["tracking_status_text"]


def test_extracts_dhl_parcel_json_window_and_latest_event():
    update = extract_tracking_update_from_json(
        carrier="dhl",
        tracking_code="3SBVMS6743345",
        payload={
            "key": "3SBVMS6743345",
            "delivery": {
                "plannedDeliveryTimeframe": "2026-05-05T17:30:00+02:00/2026-05-05T21:30:00+02:00",
            },
            "events": [
                {
                    "status": "SORTED",
                    "category": "UNDERWAY",
                    "description": "Pakket is gesorteerd",
                    "facility": {"city": "Utrecht", "countryCode": "NL"},
                },
                {
                    "status": "OUT_FOR_DELIVERY",
                    "category": "IN_DELIVERY",
                    "description": "Bezorger is onderweg",
                    "facility": {"city": "Den Haag", "countryCode": "NL"},
                },
            ],
        },
        today=date(2026, 5, 5),
    )

    assert update["status"] == "expected_today"
    assert update["expected_date"] == "2026-05-05"
    assert update["delivery_window_start"] == "17:30"
    assert update["delivery_window_end"] == "21:30"
    assert "Den Haag" in update["tracking_status_text"]
    assert update["extra"]["tracking_events"][-1]["location"] == "Den Haag, NL"


def test_extracts_dhl_problem_phase_as_unknown_with_error():
    update = extract_tracking_update_from_json(
        carrier="dhl",
        tracking_code="3SBVMS6743345",
        payload={
            "events": [
                {
                    "status": "ADDRESS_ISSUE",
                    "category": "PROBLEM",
                    "description": "Adrescontrole nodig",
                    "facility": {"city": "Den Haag", "countryCode": "NL"},
                }
            ]
        },
        today=date(2026, 5, 5),
    )

    assert update["status"] == "unknown"
    assert update["tracking_refresh_error"] == "dhl_problem"


def test_extracts_dhl_data_received_as_in_transit():
    update = extract_tracking_update_from_json(
        carrier="dhl",
        tracking_code="3SBVMS6743345",
        payload={"view": {"phaseDisplay": [{"phase": "DATA_RECEIVED", "completed": True}]}},
        today=date(2026, 5, 5),
    )

    assert update["status"] == "in_transit"


def test_extracts_fedex_json_status_location_and_window():
    update = extract_fedex_tracking_update_from_json(
        tracking_code="871354982751",
        payload={
            "output": {
                "packages": [
                    {
                        "trackingNbr": "871354982751",
                        "trackingCarrierCd": "FDXE",
                        "mainStatus": "In transit",
                        "keyStatus": "On the way",
                        "keyStatusCD": "IT",
                        "statusWithDetails": "At local FedEx facility",
                        "statusLocationAddress": {
                            "city": "DUIVEN",
                            "countryCode": "NL",
                        },
                        "estDeliveryDt": "2026-05-05T00:00:00+02:00",
                        "estDelTimeWindow": {
                            "displayEstDelTmWindowTmStart": "12:10",
                            "displayEstDelTmWindowTmEnd": "14:10",
                        },
                    }
                ]
            }
        },
        today=date(2026, 5, 5),
    )

    assert update["status"] == "expected_today"
    assert update["expected_date"] == "2026-05-05"
    assert update["delivery_window_start"] == "12:10"
    assert update["delivery_window_end"] == "14:10"
    assert "DUIVEN" in update["tracking_status_text"]
    assert update["tracking_refresh_source"] == "fedex_public_api"


def test_fedex_mail_fallback_keeps_mail_details_when_page_is_blocked():
    update = extract_fedex_tracking_update_from_mail(
        record={
            "carrier": "fedex",
            "tracking_code": "871354982751",
            "tracking_url": "https://fedex.com/fedextrack?t=871354982751",
            "status": "in_transit",
            "expected_date": "2026-05-05",
            "delivery_window_start": "00:00",
            "delivery_window_end": "00:00",
            "raw_excerpt": (
                "Uw zending van Ubiquiti International Holding B.V. is onderweg. "
                "Geplande leverdatum dinsdag, 05/05/2026. Service FedEx Priority"
            ),
        },
        error="tracking_page_blocked_or_permission",
        today=date(2026, 5, 5),
    )

    assert update["tracking_refresh_source"] == "fedex_mail_fallback"
    assert "tracking_refresh_error" not in update
    assert update["tracking_status_text"] == (
        "FedEx mail: Ubiquiti International Holding B.V. - gepland 2026-05-05 - FedEx Priority"
    )
    assert update["status"] == "expected_today"
    assert "delivery_window_start" not in update


def test_fedex_mail_fallback_delivered_mail_overrides_previous_transit_status():
    update = extract_fedex_tracking_update_from_mail(
        record={
            "carrier": "fedex",
            "tracking_code": "871354982751",
            "status": "in_transit",
            "raw_excerpt": (
                "Uw zending van Ubiquiti International Holding B.V. is afgeleverd. "
                "Service FedEx Priority"
            ),
        },
        error="tracking_page_blocked_or_permission",
        today=date(2026, 5, 5),
    )

    assert update["status"] == "delivered"
    assert update["tracking_status_text"] == "FedEx mail: Ubiquiti International Holding B.V. - FedEx Priority"


def test_normalizes_local_tracking_scraper_delivered_response():
    update = normalize_tracking_scraper_update(
        {
            "carrier": "fedex",
            "tracking_code": "871354982751",
            "status": "delivered",
            "raw_status": "Delivered",
            "tracking_status_text": "Delivered - Left at front door",
            "location": "LEIDSCHENDAM, NL",
            "expected_date": "2026-05-05",
            "events": [{"status": "Delivered", "location": "LEIDSCHENDAM, NL"}],
        },
        carrier="fedex",
        tracking_code="871354982751",
        tracking_url="https://www.fedex.com/fedextrack/?trknbr=871354982751",
        today=date(2026, 5, 5),
    )

    assert update is not None
    assert update["status"] == "delivered"
    assert update["tracking_refresh_source"] == "local_tracking_scraper"
    assert update["tracking_refresh_supported"] is True
    assert update["tracking_url"].startswith("https://www.fedex.com/fedextrack")
    assert update["expected_date"] == "2026-05-05"
    assert "LEIDSCHENDAM" in update["tracking_status_text"]
    assert update["extra"]["tracking_events"][0]["status"] == "Delivered"


def test_normalizes_local_tracking_scraper_out_for_delivery_response():
    update = normalize_tracking_scraper_update(
        {
            "carrier": "fedex",
            "tracking_code": "871354982751",
            "status": "out_for_delivery",
            "raw_status": "On FedEx vehicle for delivery",
            "delivery_window_start": "12:10",
            "delivery_window_end": "14:10",
        },
        carrier="fedex",
        tracking_code="871354982751",
        today=date(2026, 5, 5),
    )

    assert update is not None
    assert update["status"] == "expected_today"
    assert update["delivery_window_start"] == "12:10"
    assert update["delivery_window_end"] == "14:10"
