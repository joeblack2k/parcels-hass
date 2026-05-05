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

from custom_components.package_inbox.parser import parse_email


TODAY = date(2026, 4, 25)


def test_dhl_delivery_window():
    records = parse_email(
        subject="Je DHL pakket wordt vandaag bezorgd",
        sender="DHL Parcel <noreply@dhl.com>",
        text="Je pakket van Coolblue komt vandaag tussen 15:00 en 17:00. Zendingnummer JD0146001234567890",
        today=TODAY,
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "dhl"
    assert records[0]["shop"] == "Coolblue"
    assert records[0]["expected_date"] == "2026-04-25"
    assert records[0]["delivery_window_start"] == "15:00"
    assert records[0]["delivery_window_end"] == "17:00"


def test_vinted_pickup_code():
    records = parse_email(
        subject="Je Vinted pakket ligt klaar",
        sender="Vinted <no-reply@vinted.nl>",
        text="Je pakket ligt klaar bij de HEMA. De code is 0000.",
        today=date(2026, 4, 29),
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "vinted"
    assert records[0]["status"] == "ready_for_pickup"
    assert records[0]["pickup_code"] == "0000"
    assert "HEMA" in records[0]["pickup_location"]


def test_amazon_weekday_delivery_without_false_pickup():
    records = parse_email(
        subject="Besteld: Slipstop en nog 2 items",
        sender='"Amazon.nl" <auto-bevestiging@amazon.nl>',
        text=(
            "Hartelijk dank voor uw bestelling. Besteld Verzonden Onderweg voor bezorging "
            "Bezorgd Wordt bezorgd op woensdag. Verkocht bij Amazon Marketplace."
        ),
        today=TODAY,
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "amazon"
    assert records[0]["expected_date"] == "2026-04-29"
    assert records[0]["status"] == "in_transit"
    assert records[0]["pickup_location"] is None


def test_amazon_order_only_mail_is_ignored():
    records = parse_email(
        subject="Bedankt voor je bestelling",
        sender='"Amazon.nl" <auto-bevestiging@amazon.nl>',
        text=(
            "Bedankt voor je bestelling. "
            "We sturen je een bericht zodra je artikelen zijn verwerkt."
        ),
        today=TODAY,
    )

    assert records == []


def test_amazon_delivered_mail_is_delivered_without_pickup():
    records = parse_email(
        subject="Bezorgd: je pakket van Amazon",
        sender='"Amazon.nl" <shipment-tracking@amazon.nl>',
        text="Je pakket is bezorgd. Bekijk je bestelling in je Amazon-account.",
        today=TODAY,
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "amazon"
    assert records[0]["shop"] == "Amazon"
    assert records[0]["status"] == "delivered"
    assert records[0]["pickup_code"] is None
    assert records[0]["pickup_location"] is None


def test_amazon_return_mail_is_not_an_inbound_package():
    records = parse_email(
        subject="Je retourzending van Amazon is geaccepteerd",
        sender='"Amazon.nl" <retouren@amazon.nl>',
        text=(
            "Je retourzending is geaccepteerd. "
            "Print je retourlabel of bekijk de instructies voor het afgeven van je pakket."
        ),
        today=TODAY,
    )

    assert records == []


def test_ignores_unrelated_mail():
    assert parse_email(subject="Nieuwsbrief", sender="winkel", text="Alleen korting", today=TODAY) == []


def test_ignores_picnic_delivery_mail():
    records = parse_email(
        subject="Bedankt voor je bestelling!",
        sender="info@mail.picnic.nl",
        text=(
            "Picnic\n"
            "Tot dinsdag Jeroen en!\n"
            "Wat leuk dat we weer bij je langs mogen komen.\n"
            "Dinsdag 28 april\n"
            "08:00-09:50 Example Street 12"
        ),
        today=TODAY,
    )

    assert records == []


def test_ignores_vinted_support_message():
    records = parse_email(
        subject="You've got a new message",
        sender="no-reply@vinted.nl",
        text=(
            "Subject: Item is significantly not as described. "
            "You've received a message from support_vinted. "
            "Update email settings | Help"
        ),
        today=TODAY,
    )

    assert records == []


def test_vinted_go_pickup_is_not_dhl_and_extracts_code_location():
    records = parse_email(
        subject="Je pakket ligt voor je klaar #1776508411274097",
        sender="no-reply@vinted.com",
        text=(
            "Je Vinted Go-pakket is bezorgd en ligt klaar om opgehaald te worden. "
            "Ophalen vóór: 04-05-2026 "
            "Adres: Vinted Go-pakketwinkel shoeby Schoolstraat 40 Voorschoten "
            "Openingstijden Maandag 12:30-17:30 Dinsdag–Vrijdag 09:30-17:30 "
            "Afhaalcode: Scan de QR-code om je pakket op te halen of vul deze code in: 034049 "
            "Trackingnummer: 1776508411274097"
        ),
        today=TODAY,
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "vinted"
    assert records[0]["tracking_code"] == "1776508411274097"
    assert records[0]["status"] == "ready_for_pickup"
    assert records[0]["pickup_code"] == "034049"
    assert records[0]["pickup_location"] == "Vinted Go-pakketwinkel shoeby Schoolstraat 40 Voorschoten"


def test_benu_apotheek_pickup_code_location():
    records = parse_email(
        subject="Bericht van uw BENU Apotheek",
        sender="BENU <no-reply@benu.nl>",
        text=(
            "U kunt uw bestelling ophalen in de apotheek. "
            "Uw persoonlijke code is 1480. "
            "BENU Apotheek Veur te Leidschendam."
        ),
        today=TODAY,
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "apotheek"
    assert records[0]["shop"] == "BENU Apotheek"
    assert records[0]["status"] == "ready_for_pickup"
    assert records[0]["pickup_code"] == "1480"
    assert records[0]["pickup_location"] == "BENU Apotheek Veur te Leidschendam"


def test_benu_apotheek_picked_up_status():
    records = parse_email(
        subject="Uw bestelling is opgehaald",
        sender="BENU <no-reply@benu.nl>",
        text="Uw bestelling met persoonlijke code 1480 is opgehaald bij BENU Apotheek Veur te Leidschendam.",
        today=TODAY,
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "apotheek"
    assert records[0]["status"] == "picked_up"
    assert records[0]["pickup_code"] == "1480"


def test_dhl_ecommerce_jjd_tracking_code_and_url():
    records = parse_email(
        subject="We staan vandaag voor de deur tussen 17.30 - 18.30 uur (JJD000090254000059755497)",
        sender="noreply@dhlecommerce.nl",
        text=(
            "Je pakket van AMAZON EU SARL. Verwacht bezorgmoment woensdag 29 april "
            "tussen 17.30 - 18.30 uur Zendingsnummer JJD000090254000059755497 "
            "https://my.dhlecommerce.nl/go-track-trace?role=consumer-receiver&src=dhl-notification-email&tc=JJD000090254000059755497"
        ),
        today=date(2026, 4, 29),
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "dhl"
    assert records[0]["tracking_code"] == "JJD000090254000059755497"
    assert records[0]["tracking_url"].startswith("https://my.dhlecommerce.nl/go-track-trace")
    assert records[0]["expected_date"] == "2026-04-29"
    assert records[0]["delivery_window_start"] == "17:30"
    assert records[0]["delivery_window_end"] == "18:30"


def test_fedex_delivery_planned_date_tracking_url_and_shop():
    records = parse_email(
        subject="Uw zending is onderweg 871354982751",
        sender="FedEx Express <noreply@fedex.com>",
        text=(
            "Uw zending van Ubiquiti International Holding B.V. is onderweg. "
            "Geplande leverdatum dinsdag, 05/05/2026 "
            "https://fedex.com/fedextrack?t=871354982751&l=nl_NL&et=mail "
            "Tracking-id 871354982751 Verzenddatum 2026-05-04 "
            "Aantal stukken 1 Service FedEx Priority"
        ),
        today=date(2026, 5, 4),
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "fedex"
    assert records[0]["shop"] == "Ubiquiti International Holding B.V"
    assert records[0]["tracking_code"] == "871354982751"
    assert records[0]["tracking_url"].startswith("https://fedex.com/fedextrack")
    assert records[0]["expected_date"] == "2026-05-05"
    assert records[0]["status"] == "in_transit"


def test_fedex_trknbr_url_extracts_tracking_code():
    records = parse_email(
        subject="Uw zending is onderweg",
        sender="FedEx Express <noreply@fedex.com>",
        text=(
            "Wilt u iets wijzigen aan deze aflevering? "
            "https://www.fedex.com/fedextrack/?trknbr=871354982751&mydelivery=fdmi"
        ),
        today=date(2026, 5, 5),
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "fedex"
    assert records[0]["tracking_code"] == "871354982751"
    assert records[0]["tracking_url"].startswith("https://www.fedex.com/fedextrack")


def test_chronopost_mail_extracts_tracking_code_and_url():
    records = parse_email(
        subject="Your parcel is on its way XU152297803JF",
        sender="Chronopost <avisage-ne-pas-repondre@chronopost.fr>",
        text=(
            "Dear Customer, Your parcel XU152297803JF / 343431803222365 "
            "is being handled in our network. "
            "Chronopost customer service "
            "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF"
        ),
        today=date(2026, 5, 5),
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "chronopost"
    assert records[0]["tracking_code"] == "XU152297803JF"
    assert records[0]["tracking_url"].startswith("https://www.chronopost.fr/tracking-no-cms/suivi-page")
    assert records[0]["status"] == "in_transit"


def test_postnl_tntp_style_tracking_code():
    records = parse_email(
        subject="Je PostNL pakket is onderweg",
        sender="PostNL <noreply@postnl.nl>",
        text="Je pakket van De Lijsten Fabriek heeft barcode KG12345678 en is onderweg.",
        today=date(2026, 5, 5),
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "postnl"
    assert records[0]["tracking_code"] == "KG12345678"
    assert records[0]["status"] == "in_transit"


def test_postnl_afgeleverd_mail_is_delivered_without_pickup_code():
    records = parse_email(
        subject="Afgeleverd: je pakket van De Lijsten Fabriek",
        sender="PostNL <notificatie@edm.postnl.nl>",
        text=(
            "Wij zijn PostNL en we hebben iets voor je. "
            "Afgeleverd: je pakket van De Lijsten Fabriek. "
            "Trackingcode 3SBVMS6743345 "
            "https://tracking.postnl.nl/track-and-trace/3SBVMS6743345--"
        ),
        today=date(2026, 5, 5),
    )

    assert len(records) == 1
    assert records[0]["carrier"] == "postnl"
    assert records[0]["shop"] == "De Lijsten Fabriek"
    assert records[0]["tracking_code"] == "3SBVMS6743345"
    assert records[0]["status"] == "delivered"
    assert records[0]["pickup_code"] is None
    assert records[0]["pickup_location"] is None


def test_does_not_turn_sentence_after_tracking_code_into_code_or_due_date():
    records = parse_email(
        subject="Re: Bestelling",
        sender="info@billenboetiek.nl",
        text=(
            "Jullie bestelling is goed binnengekomen en gaat mee met DHL. "
            "Hij kan door de brievenbus, dus voor vrijdag zou goed moeten komen. "
            "De tracking code zou dus ook naar dat adres moeten gaan."
        ),
        today=date(2026, 4, 29),
    )

    assert records == []
