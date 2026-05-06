from addons.parcels_fedex_scraper.app.main import (
    normalize_chronopost_text,
    settings_from_options,
    vinted_configured,
    vinted_login_blocker,
)


def test_sidecar_settings_include_disabled_vinted_auto_login_by_default():
    settings = settings_from_options({})

    assert settings.vinted_auto_login is False
    assert settings.vinted_login_on_start is True
    assert settings.vinted_login_interval_hours == 22
    assert vinted_configured(settings) is False


def test_sidecar_settings_enable_vinted_auto_login_from_options():
    settings = settings_from_options(
        {
            "vinted_auto_login": True,
            "vinted_email": "user@example.test",
            "vinted_password": "secret",
            "vinted_login_interval_hours": 12,
            "vinted_login_on_start": False,
        }
    )

    assert settings.vinted_auto_login is True
    assert settings.vinted_login_on_start is False
    assert settings.vinted_login_interval_hours == 12
    assert vinted_configured(settings) is True


def test_vinted_login_blocker_detects_manual_challenges():
    assert vinted_login_blocker("Please solve the captcha to continue") == "captcha_required"
    assert vinted_login_blocker("Enter your verification code") == "two_factor_required"
    assert vinted_login_blocker("Ongeldig e-mailadres of verkeerd wachtwoord") == "invalid_credentials"


def test_chronopost_sidecar_extracts_pickup_location():
    update = normalize_chronopost_text(
        """
        Votre colis est disponible au point relais Pickup
        HEMA LEIDSCHENDAM
        DAMLAAN 44
        2265 AP LEIDSCHENDAM
        Historique de votre colis
        """,
        tracking_code="XU152297803JF",
        tracking_url="https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
    )

    assert update["carrier"] == "chronopost"
    assert update["status"] == "ready_for_pickup"
    assert update["pickup_location"] == "HEMA LEIDSCHENDAM, DAMLAAN 44, 2265 AP LEIDSCHENDAM"
    assert update["tracking_status_text"].startswith("Afhalen bij HEMA")
    assert "delivery_window_start" not in update


def test_chronopost_sidecar_does_not_mark_relay_delivery_as_home_delivery():
    update = normalize_chronopost_text(
        "Votre colis sera livre dans le point relais Pickup HEMA LEIDSCHENDAM le 7 mai.",
        tracking_code="XU152297803JF",
        tracking_url="https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
    )

    assert update["status"] == "unknown"
    assert "pickup_location" not in update
    assert update["expected_date"] == "2026-05-07"


def test_chronopost_sidecar_uses_latest_in_transit_event_over_pickup_point_field():
    update = normalize_chronopost_text(
        """
        Tuesday 05/05/2026
        11:20 PM
        HUB CHILLY MAZARIN CHRONOPOST
        Shipment in transit
        Tuesday 05/05/2026
        11:18 PM
        HUB CHILLY MAZARIN CHRONOPOST
        Outbound linehaul scan
        Tuesday 05/05/2026 12:09 PM
        NOGENT SUR OISE - FR - KALTEL
        Parcel handed over from Pickup point to the driver
        Type of collection point : Chronopost Relais Point
        Saturday 05/02/2026 07:08 AM
        Web Services
        Shipment in preparation to be shipped
        Partner number : GEO/343431803222365
        Pick up point : DROOMVISIE - SCHOOLSTRAAT 109A -
        2251 BG - VOORSCHOTEN - NL
        I wish to receive my tracking orders by e-mail
        """,
        tracking_code="XU152297803JF",
        tracking_url="https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
    )

    assert update["status"] == "in_transit"
    assert update["tracking_status_text"] == "Shipment in transit - HUB CHILLY MAZARIN CHRONOPOST"
    assert "pickup_location" not in update
    assert "expected_date" not in update


def test_chronopost_sidecar_ignores_collection_type_normal_as_location():
    update = normalize_chronopost_text(
        """
        Parcel handed over from Pickup point to the driver
        Type of collection point : Chronopost Relais Point
        Collection point type : NORMAL
        Shipment in transit
        """,
        tracking_code="XU152297803JF",
        tracking_url="https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
    )

    assert "pickup_location" not in update
    assert update["status"] == "in_transit"


def test_chronopost_sidecar_does_not_use_history_location_as_pickup_address():
    update = normalize_chronopost_text(
        """
        Tuesday 05/05/2026 12:09 PM
        NOGENT SUR OISE - FR - KALTEL
        Parcel handed over from Pickup point to the driver
        Type of collection point : Chronopost Relais Point
        Saturday 05/02/2026 04:15 PM
        NOGENT SUR OISE - FR - KALTEL
        Shipment handed over by shipper
        Type of collection point : Chronopost Relais Point
        """,
        tracking_code="XU152297803JF",
        tracking_url="https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
    )

    assert "pickup_location" not in update


def test_chronopost_sidecar_treats_delivered_to_pickup_point_as_pickup_ready():
    update = normalize_chronopost_text(
        """
        Livré au point relais
        samedi 02/05/2026, 16:15 NOGENT SUR OISE - FR - KALTEL
        """,
        tracking_code="XU152297803JF",
        tracking_url="https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
    )

    assert update["status"] == "ready_for_pickup"
    assert "pickup_location" not in update
