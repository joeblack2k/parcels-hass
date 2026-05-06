from addons.parcels_fedex_scraper.app.main import (
    clean_vinted_cookie_string,
    dedupe_vinted_records,
    normalize_chronopost_text,
    settings_from_options,
    vinted_record_from_text,
    vinted_record_from_api_package,
    vinted_records_from_json,
    vinted_configured,
    vinted_login_blocker,
    vinted_login_needs_manual_attention,
    vinted_package_from_conversation,
    vinted_page_looks_logged_out,
)


def test_sidecar_settings_include_disabled_vinted_auto_login_by_default():
    settings = settings_from_options({})

    assert settings.vinted_auto_login is False
    assert settings.vinted_login_on_start is True
    assert settings.vinted_login_interval_hours == 22
    assert settings.vinted_accounts == ()
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
    assert len(settings.vinted_accounts) == 1
    assert settings.vinted_accounts[0].key == "account_1"
    assert settings.vinted_accounts[0].session_cookie == ""
    assert str(settings.vinted_accounts[0].profile_dir).endswith("/vinted")
    assert vinted_configured(settings) is True


def test_sidecar_settings_support_two_vinted_accounts():
    settings = settings_from_options(
        {
            "vinted_auto_login": True,
            "vinted_email": "first@example.test",
            "vinted_password": "secret-1",
            "vinted_email_2": "second@example.test",
            "vinted_password_2": "secret-2",
        }
    )

    assert [account.key for account in settings.vinted_accounts] == ["account_1", "account_2"]
    assert str(settings.vinted_accounts[0].profile_dir).endswith("/vinted/account_1")
    assert str(settings.vinted_accounts[1].profile_dir).endswith("/vinted/account_2")
    assert vinted_configured(settings) is True


def test_sidecar_settings_support_vinted_session_cookie_without_password():
    settings = settings_from_options(
        {
            "vinted_auto_login": True,
            "vinted_session_cookie": "access_token_web=abc; refresh_token_web=def",
        }
    )

    assert len(settings.vinted_accounts) == 1
    assert settings.vinted_accounts[0].email == ""
    assert settings.vinted_accounts[0].password == ""
    assert settings.vinted_accounts[0].session_cookie == "access_token_web=abc; refresh_token_web=def"
    assert vinted_configured(settings) is True


def test_vinted_cookie_sanitizer_keeps_only_allowed_login_cookies():
    cookie = clean_vinted_cookie_string(
        "access_token_web=abc; refresh_token_web=def; sessionid=drop; "
        "_vinted_fr_session=ghi; datadome=jkl"
    )

    assert "access_token_web=abc" in cookie
    assert "refresh_token_web=def" in cookie
    assert "_vinted_fr_session=ghi" in cookie
    assert "datadome=jkl" in cookie
    assert "sessionid=drop" not in cookie


def test_vinted_login_blocker_detects_manual_challenges():
    assert vinted_login_blocker("Please solve the captcha to continue") == "captcha_required"
    assert vinted_login_blocker("Enter your verification code") == "two_factor_required"
    assert vinted_login_blocker("Ongeldig e-mailadres of verkeerd wachtwoord") == "invalid_credentials"


def test_vinted_browser_login_form_failures_do_not_block_api_scrape():
    assert vinted_login_needs_manual_attention("captcha_required") is True
    assert vinted_login_needs_manual_attention("two_factor_required") is True
    assert vinted_login_needs_manual_attention("login_form_not_found") is False
    assert vinted_login_needs_manual_attention("login_required") is False


def test_vinted_logged_out_landing_page_is_not_a_valid_session():
    assert vinted_page_looks_logged_out(
        "artikelen registreren | inloggen verkoop nu word lid en verkoop tweedehands kleding "
        "ga verder met google heb je al een account? inloggen"
    )


def test_vinted_text_extracts_pickup_and_chronopost_reference():
    record = vinted_record_from_text(
        (
            "Je pakket ligt klaar bij DROOMVISIE Schoolstraat 109A Voorschoten. "
            "Afhaalcode: 034049. Chronopost tracking XU152297803JF. "
            "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF"
        ),
        account_key="account_2",
        source_url="https://www.vinted.nl/inbox/123",
    )

    assert record is not None
    assert record["carrier"] == "vinted"
    assert record["status"] == "ready_for_pickup"
    assert record["pickup_code"] == "034049"
    assert record["pickup_location"] == "DROOMVISIE Schoolstraat 109A Voorschoten"
    assert record["extra"]["carrier_tracking"] == {
        "carrier": "chronopost",
        "tracking_code": "XU152297803JF",
        "tracking_url": "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF",
    }


def test_vinted_text_can_correct_stale_pickup_to_in_transit():
    record = vinted_record_from_text(
        "Vinted pakket is verzonden en onderweg. Chronopost tracking XU152297803JF.",
        account_key="account_1",
        source_url="https://www.vinted.nl/inbox/456",
    )

    assert record is not None
    assert record["status"] == "in_transit"
    assert "pickup_location" not in record
    assert record["extra"]["carrier_tracking"]["carrier"] == "chronopost"


def test_vinted_text_does_not_treat_pickup_point_destination_as_ready():
    record = vinted_record_from_text(
        "Vinted pakket. Pickup point: DROOMVISIE Schoolstraat 109A Voorschoten. "
        "Chronopost tracking XU152297803JF.",
        account_key="account_1",
        source_url="https://www.vinted.nl/inbox/789",
    )

    assert record is not None
    assert record["status"] == "unknown"
    assert "pickup_location" not in record
    assert record["extra"]["carrier_tracking"]["carrier"] == "chronopost"


def test_vinted_json_records_are_normalized_and_deduped():
    records = vinted_records_from_json(
        {
            "transactions": [
                {
                    "id": 123,
                    "status": "ready for pickup",
                    "shipment": {
                        "carrier": "Chronopost",
                        "tracking_code": "XU152297803JF",
                        "pickup_code": "034049",
                    },
                    "pickup_point": "DROOMVISIE Schoolstraat 109A Voorschoten",
                }
            ]
        },
        account_key="account_2",
        source_url="https://www.vinted.nl/api/v2/transactions",
    )

    deduped = dedupe_vinted_records(records)

    assert len(deduped) == 1
    assert deduped[0]["status"] == "ready_for_pickup"
    assert deduped[0]["extra"]["vinted_id"] == "123"


def test_vinted_api_conversation_extracts_structured_chronopost_pickup():
    package = vinted_package_from_conversation(
        {"id": 77, "last_message_at": "2026-05-06T09:20:00Z"},
        {
            "conversation": {
                "id": 77,
                "transaction": {
                    "id": 123,
                    "shipment": {
                        "id": 456,
                        "tracking_status": "ready_for_pickup",
                        "carrier_name": "Chronopost",
                        "tracking_code": "XU152297803JF",
                        "pickup_point": "DROOMVISIE Schoolstraat 109A Voorschoten",
                        "pickup_code": "034049",
                        "expires_at": "2026-05-12T18:00:00Z",
                    },
                    "item": {"title": "Jas"},
                },
            }
        },
    )
    assert package is not None

    record = vinted_record_from_api_package(
        package,
        account_key="account_2",
        source_url="https://www.vinted.nl/inbox/77",
    )

    assert record is not None
    assert record["tracking_refresh_source"] == "vinted_sidecar_api"
    assert record["status"] == "ready_for_pickup"
    assert record["pickup_location"] == "DROOMVISIE Schoolstraat 109A Voorschoten"
    assert record["pickup_code"] == "034049"
    assert record["extra"]["carrier_tracking"]["carrier"] == "chronopost"
    assert record["extra"]["carrier_tracking"]["tracking_code"] == "XU152297803JF"
    assert record["extra"]["vinted_thread_id"] == "77"


def test_vinted_api_conversation_extracts_product_eta_range_and_timeline():
    package = vinted_package_from_conversation(
        {"id": 99, "last_message_at": "2026-05-06T14:50:00Z"},
        {
            "conversation": {
                "id": 99,
                "other_user": {"login": "bruijna1981"},
                "transaction": {
                    "id": 19631936553,
                    "shipment": {
                        "id": 1778051829299958,
                        "tracking_status": "in transit",
                        "tracking_code": "1778051829299958",
                        "expected_delivery_from": "2026-05-11",
                        "expected_delivery_to": "2026-05-13",
                        "events": [
                            {"title": "Onderweg", "created_at": "2026-05-06T14:49:00+02:00"},
                            {"title": "Verzonden", "created_at": "2026-05-06T10:10:00+02:00"},
                            {
                                "title": "Trackingcode aangemaakt",
                                "created_at": "2026-05-06T09:17:00+02:00",
                            },
                        ],
                    },
                    "item": {"title": "5 artikelen"},
                },
            }
        },
    )

    assert package is not None
    assert package["status"] == "in_transit"
    assert package["item_title"] == "5 artikelen"
    assert package["other_party"] == "bruijna1981"
    assert package["expected_date"] == "2026-05-11"
    assert package["expected_date_to"] == "2026-05-13"
    assert package["tracking_events"][0]["status"] == "Onderweg"

    record = vinted_record_from_api_package(
        package,
        account_key="account_2",
        source_url="https://www.vinted.nl/inbox/99",
    )

    assert record is not None
    assert record["tracking_code"] == "1778051829299958"
    assert record["expected_date"] == "2026-05-11"
    assert record["tracking_status_text"].startswith("5 artikelen - verwacht 2026-05-11")
    assert record["extra"]["vinted_item_title"] == "5 artikelen"
    assert record["extra"]["vinted_other_party"] == "bruijna1981"
    assert record["extra"]["expected_date_end"] == "2026-05-13"
    assert record["extra"]["tracking_events"][0]["timestamp"] == "2026-05-06T14:49:00+02:00"


def test_vinted_text_extracts_app_visible_product_eta_and_events():
    record = vinted_record_from_text(
        """
        bruijna1981
        5 artikelen
        15,60
        Bestelling verzonden.
        Je bestelling is onderweg! Verwachte levertijd: mei 11 - mei 13
        Pakket volgen
        Trackingnummer 1778051829299958
        Onderweg 06-05-2026, 14:49
        Verzonden 06-05-2026, 10:10
        Trackingcode aangemaakt - 1778051829299958 06-05-2026, 09:17
        """,
        account_key="account_1",
        source_url="https://www.vinted.nl/inbox/99",
    )

    assert record is not None
    assert record["status"] == "in_transit"
    assert record["tracking_code"] == "1778051829299958"
    assert record["expected_date"] == "2026-05-11"
    assert record["extra"]["expected_date_end"] == "2026-05-13"
    assert record["extra"]["vinted_item_title"] == "5 artikelen"
    assert record["extra"]["vinted_other_party"] == "bruijna1981"
    assert [event["status"] for event in record["extra"]["tracking_events"]] == [
        "Onderweg",
        "Verzonden",
        "Trackingcode aangemaakt",
    ]


def test_vinted_api_conversation_can_downgrade_to_in_transit():
    package = vinted_package_from_conversation(
        {"id": 88},
        {
            "conversation": {
                "id": 88,
                "transaction": {
                    "id": 124,
                    "shipment": {
                        "id": 457,
                        "tracking_status": "in transit",
                        "carrier_name": "Chronopost",
                        "tracking_code": "XU152297803JF",
                        "pickup_point": "DROOMVISIE Schoolstraat 109A Voorschoten",
                    },
                },
            }
        },
    )
    assert package is not None

    record = vinted_record_from_api_package(
        package,
        account_key="account_2",
        source_url="https://www.vinted.nl/inbox/88",
    )

    assert record is not None
    assert record["status"] == "in_transit"
    assert "pickup_location" not in record
    assert record["extra"]["carrier_tracking"]["carrier"] == "chronopost"


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
