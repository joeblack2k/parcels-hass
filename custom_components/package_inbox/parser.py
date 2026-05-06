"""Mail parser for parcel notifications.

This module is intentionally Home Assistant independent so it can be tested
without starting HA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import hashlib
import html
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .carrier_rules import (
    detect_carrier as detect_rule_carrier,
    extract_tracking_code as extract_rule_tracking_code,
    extract_tracking_code_from_url as extract_rule_tracking_code_from_url,
    valid_tracking_code as valid_rule_tracking_code,
)
from .const import (
    STATUS_DELIVERED,
    STATUS_EXPECTED_TODAY,
    STATUS_IN_TRANSIT,
    STATUS_PICKED_UP,
    STATUS_READY_FOR_PICKUP,
    STATUS_UNKNOWN,
)


PACKAGE_WORDS = (
    "pakket",
    "pakje",
    "zending",
    "track",
    "tracking",
    "track & trace",
    "track and trace",
    "parcel",
    "shipment",
    "delivery",
    "bezorg",
    "afhaal",
    "ophalen",
    "pickup",
    "pakketpunt",
    "pakketshop",
    "apotheek",
    "benu",
    "medicijn",
    "medicatie",
    "persoonlijke code",
)

CARRIER_KEYWORDS = {
    "postnl": ("postnl", "post nl", "mijn postnl", "3s"),
    "dhl": ("dhl", "jvgl", "jjd"),
    "dpd": ("dpd", "dpd group"),
    "gls": ("gls", "gls netherlands", "glsnl"),
    "fedex": ("fedex", "fedex express"),
    "chronopost": ("chronopost", "chronopost.fr", "chrono"),
    "ups": ("ups", "united parcel service", "1z"),
    "trunkrs": ("trunkrs",),
    "homerr": ("homerr",),
    "cycloon": ("cycloon", "cycloon fietskoeriers"),
    "instabox": ("instabox", "red je pakketje", "redjep"),
    "transmission": ("transmission", "trans-mission", "trans mission"),
    "dachser": ("dachser",),
    "dynalogic": ("dynalogic", "mydynalogic"),
    "gofo": ("gofo", "gofo express"),
    "dragonfly": ("dragonfly", "dragonfly netherlands"),
    "amazon": ("amazon",),
    "vinted": ("vinted",),
    "apotheek": ("benu", "apotheek", "medicijn", "medicatie"),
}

SHOP_HINTS = {
    "bol.com": ("bol.com", "bol ", "bol.com bestelling"),
    "Coolblue": ("coolblue",),
    "Zalando": ("zalando",),
    "Amazon": ("amazon",),
    "Vinted": ("vinted",),
    "BENU Apotheek": ("benu", "benu apotheek"),
    "Apotheek": ("apotheek",),
    "Ubiquiti": ("ubiquiti", "ubiquiti international"),
    "HEMA": ("hema",),
    "MediaMarkt": ("mediamarkt",),
    "Marktplaats": ("marktplaats",),
}

IGNORE_SENDER_PATTERNS = (
    "picnic.nl",
    "ah.nl",
    "jumbo.com",
    "crisp.nl",
    "flink.com",
    "thuisbezorgd.nl",
    "ubereats.com",
    "deliveroo.",
)

WEEKDAYS = {
    "maandag": 0,
    "monday": 0,
    "dinsdag": 1,
    "tuesday": 1,
    "woensdag": 2,
    "wednesday": 2,
    "donderdag": 3,
    "thursday": 3,
    "vrijdag": 4,
    "friday": 4,
    "zaterdag": 5,
    "saturday": 5,
    "zondag": 6,
    "sunday": 6,
}


@dataclass(slots=True)
class ParsedPackage:
    """A normalized package parsed from a mail or external source."""

    carrier: str = "unknown"
    shop: str | None = None
    tracking_code: str | None = None
    status: str = STATUS_UNKNOWN
    expected_date: str | None = None
    delivery_window_start: str | None = None
    delivery_window_end: str | None = None
    pickup_location: str | None = None
    pickup_code: str | None = None
    tracking_url: str | None = None
    source: str = "imap"
    confidence: str = "low"
    message_id: str | None = None
    imap_uid: str | None = None
    raw_excerpt: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dictionary."""
        return {
            "carrier": self.carrier,
            "shop": self.shop,
            "tracking_code": self.tracking_code,
            "status": self.status,
            "expected_date": self.expected_date,
            "delivery_window_start": self.delivery_window_start,
            "delivery_window_end": self.delivery_window_end,
            "pickup_location": self.pickup_location,
            "pickup_code": self.pickup_code,
            "tracking_url": self.tracking_url,
            "source": self.source,
            "confidence": self.confidence,
            "message_id": self.message_id,
            "imap_uid": self.imap_uid,
            "raw_excerpt": self.raw_excerpt,
            "extra": self.extra,
        }


def clean_text(value: str | None) -> str:
    """Normalize plain/html mail text."""
    if not value:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_likely_package_email(subject: str | None, sender: str | None, text: str | None) -> bool:
    """Return True when an email looks relevant enough to fetch/parse."""
    haystack = clean_text("\n".join([subject or "", sender or "", text or ""])).lower()
    if any(word in haystack for word in PACKAGE_WORDS):
        return True
    return any(any(keyword in haystack for keyword in keys) for keys in CARRIER_KEYWORDS.values())


def parse_email(
    *,
    subject: str | None = None,
    sender: str | None = None,
    text: str | None = None,
    message_id: str | None = None,
    imap_uid: str | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Parse a package email into normalized package records."""
    today = today or date.today()
    body = clean_text(text)
    subject_clean = clean_text(subject)
    sender_clean = clean_text(sender)
    combined = "\n".join(part for part in (subject_clean, sender_clean, body) if part)

    if _should_ignore_email(subject_clean, sender_clean, body):
        return []

    if not is_likely_package_email(subject_clean, sender_clean, body):
        return []

    carrier = _detect_carrier(combined)
    tracking_code = _extract_tracking_code(combined, carrier)
    tracking_url = _extract_tracking_url(combined, tracking_code, carrier)
    carrier_tracking = _extract_embedded_carrier_tracking(combined, primary_carrier=carrier)
    expected_date = _extract_expected_date(combined, today)
    window_start, window_end = _extract_time_window(combined)
    status = _extract_status(combined, expected_date, today)
    pickup_code = _extract_pickup_code(combined, status)
    pickup_location = _extract_pickup_location(combined)
    if status != STATUS_READY_FOR_PICKUP:
        pickup_location = None
    if status == STATUS_READY_FOR_PICKUP:
        window_start, window_end = (None, None)
    shop = _extract_shop(combined, sender_clean, carrier)

    confidence = "low"
    if carrier != "unknown" and (tracking_code or expected_date or pickup_code):
        confidence = "high"
    elif carrier != "unknown" or tracking_code or pickup_code:
        confidence = "medium"

    if (
        status == STATUS_UNKNOWN
        and not tracking_code
        and not expected_date
        and not window_start
        and not window_end
        and not pickup_code
        and not pickup_location
    ):
        return []

    record = ParsedPackage(
        carrier=carrier,
        shop=shop,
        tracking_code=tracking_code,
        status=status,
        expected_date=expected_date,
        delivery_window_start=window_start,
        delivery_window_end=window_end,
        pickup_location=pickup_location,
        pickup_code=pickup_code,
        tracking_url=tracking_url,
        source="imap",
        confidence=confidence,
        message_id=message_id,
        imap_uid=imap_uid,
        raw_excerpt=_excerpt(combined),
        extra={"carrier_tracking": carrier_tracking} if carrier_tracking else {},
    )
    return [record.as_dict()]


def _should_ignore_email(subject: str, sender: str, text: str) -> bool:
    """Skip known non-package senders that are handled elsewhere."""
    sender_lower = sender.lower()
    if any(pattern in sender_lower for pattern in IGNORE_SENDER_PATTERNS):
        return True

    lowered = "\n".join((subject, sender, text)).lower()
    if any(pattern in lowered for pattern in IGNORE_SENDER_PATTERNS):
        return True
    if "amazon" in sender_lower or "amazon" in lowered:
        if _looks_like_amazon_return_mail(lowered):
            return True
        if _looks_like_amazon_order_only_mail(lowered):
            return True
    if ("vinted" in sender_lower or "vinted" in lowered) and _looks_like_vinted_order_only_mail(lowered):
        return True
    if "vinted" in sender_lower and any(
        term in lowered
        for term in (
            "you've got a new message",
            "you have a new message",
            "support_vinted",
            "not as described",
            "update email settings",
        )
    ) and not any(
        term in lowered
        for term in (
            "pakket",
            "package",
            "parcel",
            "zending",
            "tracking",
            "pickup",
            "afhalen",
            "ligt klaar",
        )
    ):
        return True
    return False


def _looks_like_vinted_order_only_mail(lowered: str) -> bool:
    if not any(
        term in lowered
        for term in (
            "je betaling is ontvangen",
            "bestelbevestiging",
            "transactienummer",
            "je bon voor",
        )
    ):
        return False
    return any(
        term in lowered
        for term in (
            "we laten het je weten zodra",
            "verkopers hebben tot",
            "zodra het pakket is verzonden",
            "zodra de bestelling is verzonden",
        )
    )


def _looks_like_amazon_return_mail(lowered: str) -> bool:
    return any(
        term in lowered
        for term in (
            "retourzending",
            "retouraanvraag",
            "retourverzoek",
            "retourlabel",
            "terugbetaling",
            "je retour is geaccepteerd",
            "return accepted",
            "return request",
            "return label",
            "refund",
        )
    )


def _looks_like_amazon_order_only_mail(lowered: str) -> bool:
    if not any(
        term in lowered
        for term in (
            "bedankt voor uw bestelling",
            "bedankt voor je bestelling",
            "bestelling geplaatst",
            "order confirmation",
            "thanks for your order",
        )
    ):
        return False
    return not any(
        term in lowered
        for term in (
            "wordt bezorgd",
            "onderweg voor bezorging",
            "out for delivery",
            "is onderweg",
            "is verzonden",
            "has shipped",
            "shipped",
            "tracking",
            "track & trace",
            "is bezorgd",
            "afgeleverd",
            "delivered",
            "pickup",
            "afhalen",
        )
    )


def stable_key(record: dict[str, Any]) -> str:
    """Create a stable dedupe key for a normalized package record."""
    carrier = _slug(record.get("carrier") or "unknown")
    tracking = _slug(record.get("tracking_code") or "")
    if tracking:
        return f"{carrier}:{tracking}"

    pickup_code = _slug(record.get("pickup_code") or "")
    if pickup_code:
        return f"pickup:{carrier}:{pickup_code}"

    message_id = _slug(record.get("message_id") or "")
    if message_id:
        return f"message:{message_id}"

    imap_uid = _slug(record.get("imap_uid") or "")
    if imap_uid:
        return f"imap:{imap_uid}"

    digest_source = "|".join(
        str(record.get(key) or "")
        for key in (
            "carrier",
            "shop",
            "status",
            "expected_date",
            "delivery_window_start",
            "pickup_location",
            "pickup_code",
            "raw_excerpt",
        )
    )
    return "digest:" + hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:16]


def _detect_carrier(value: str) -> str:
    lowered = value.lower()
    if "vinted" in lowered:
        return "vinted"
    if "benu" in lowered or "apotheek" in lowered:
        return "apotheek"
    rule_carrier = detect_rule_carrier(value)
    if rule_carrier != "unknown":
        return rule_carrier
    for carrier, keywords in CARRIER_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return carrier
    if re.search(r"\b(?:JJD|JD|JVGL)[A-Z0-9]{10,30}\b", value.replace(" ", ""), re.IGNORECASE):
        return "dhl"
    return "unknown"


def _extract_tracking_code(value: str, carrier: str) -> str | None:
    rule_code = extract_rule_tracking_code(value, carrier)
    if rule_code:
        return rule_code

    compact = value.replace(" ", "")
    carrier_patterns = {
        "postnl": (r"(?<![A-Z0-9])3S[A-Z0-9]{8,18}(?![A-Z0-9])",),
        "dhl": (r"(?<![A-Z0-9])(?:JJD|JD|JVGL)[A-Z0-9]{10,30}(?![A-Z0-9])",),
        "dpd": (r"(?<!\d)\d{12,16}(?!\d)",),
        "gls": (r"(?<![A-Z0-9])[A-Z0-9]{8,14}(?![A-Z0-9])",),
        "fedex": (r"(?<!\d)\d{10,15}(?!\d)",),
        "chronopost": (
            r"(?<![A-Z0-9])[A-Z]{2}\d{9}[A-Z]{2}(?![A-Z0-9])",
            r"(?<!\d)\d{13,15}(?!\d)",
        ),
        "ups": (r"(?<![A-Z0-9])1Z[A-Z0-9]{16}(?![A-Z0-9])",),
        "trunkrs": (r"(?<!\d)4\d{7,15}(?!\d)",),
        "homerr": (r"(?<![A-Z0-9])HMR[A-Z0-9]{14}(?![A-Z0-9])",),
        "cycloon": (r"(?<![A-Z0-9])FKS[A-Z0-9]{6,24}(?![A-Z0-9])",),
        "transmission": (r"(?<![A-Z0-9])T[A-Z0-9]{14}(?![A-Z0-9])",),
        "gofo": (r"(?<![A-Z0-9])GF\d{10,20}(?![A-Z0-9])",),
    }
    for pattern in carrier_patterns.get(carrier, ()):
        for source in (value, compact):
            match = re.search(pattern, source, re.IGNORECASE)
            if match:
                code = match.group(0).upper()
                if _valid_tracking_code(code, carrier):
                    return code

    url_code = _extract_tracking_code_from_url(value)
    if url_code and _valid_tracking_code(url_code, carrier):
        return url_code

    generic = re.search(
        r"(?i)(?:trackingnummer|tracking\s*number|tracking\b|track\s*(?:&|and)?\s*trace|zendingnummer|pakketnummer|parcel\s*number|shipment\s*number)"
        r"[:\s#-]{0,12}([A-Z0-9][A-Z0-9 -]{5,30})",
        value,
    )
    if generic:
        code = re.sub(r"[^A-Z0-9]", "", generic.group(1).upper())
        if 6 <= len(code) <= 32 and _valid_tracking_code(code, carrier):
            return code
    return None


def _extract_tracking_code_from_url(value: str) -> str | None:
    rule_code = extract_rule_tracking_code_from_url(value)
    if rule_code:
        return rule_code

    for url in _extract_urls(value):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        for key in (
            "tc",
            "t",
            "tracking-id",
            "tracking_id",
            "parcelNumber",
            "parcelnumber",
            "match",
            "key",
            "listeNumerosLT",
            "listenumeroslt",
            "shipment",
            "shipmentNumber",
            "shipmentnumber",
            "zending",
            "zendingnummer",
        ):
            for item in query.get(key, []):
                code = re.sub(r"[^A-Z0-9]", "", unquote(item).upper())
                if 6 <= len(code) <= 40:
                    return code
    return None


def _extract_tracking_url(value: str, tracking_code: str | None, carrier: str | None = None) -> str | None:
    code = (tracking_code or "").lower()
    for url in _extract_urls(value):
        normalized = url.lower()
        if code and (code in normalized or f"tc={code}" in normalized or f"tracking-id={code}" in normalized):
            return url
    for url in _extract_urls(value):
        detected = detect_rule_carrier(url)
        if carrier and detected == carrier:
            return url
        host = urlparse(url).netloc.lower()
        if carrier and carrier in {
            "trunkrs",
            "homerr",
            "cycloon",
            "instabox",
            "transmission",
            "dachser",
            "dynalogic",
            "gofo",
            "dragonfly",
            "ups",
        } and any(keyword in host for keyword in CARRIER_KEYWORDS.get(carrier, ())):
            return url
    return None


def _extract_embedded_carrier_tracking(
    value: str,
    *,
    primary_carrier: str,
) -> dict[str, str] | None:
    """Extract a carrier reference from a Vinted mail while keeping Vinted as source."""
    if primary_carrier != "vinted":
        return None

    for url in _extract_urls(value):
        carrier = detect_rule_carrier(url)
        if carrier in {"unknown", "vinted"}:
            continue
        code = extract_rule_tracking_code_from_url(url, carrier) or extract_rule_tracking_code(url, carrier)
        if code and valid_rule_tracking_code(code, carrier):
            return {
                "carrier": carrier,
                "tracking_code": code,
                "tracking_url": url,
            }

    lowered = value.lower()
    for carrier, keywords in CARRIER_KEYWORDS.items():
        if carrier in {"vinted", "amazon", "apotheek", "unknown"}:
            continue
        if not any(keyword in lowered for keyword in keywords):
            continue
        code = extract_rule_tracking_code(value, carrier)
        if code and valid_rule_tracking_code(code, carrier):
            reference = {
                "carrier": carrier,
                "tracking_code": code,
            }
            tracking_url = _extract_tracking_url(value, code, carrier)
            if tracking_url:
                reference["tracking_url"] = tracking_url
            return reference
    return None


def _extract_urls(value: str) -> list[str]:
    decoded = html.unescape(value)
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s\"'<>]+", decoded):
        urls.append(match.group(0).rstrip(").,;]"))
    return urls


def _valid_tracking_code(code: str, carrier: str) -> bool:
    if not any(char.isdigit() for char in code):
        return False
    bogus_fragments = (
        "CODEZOU",
        "TRACKINGCODE",
        "NAARDATADRES",
        "DATADRES",
        "GOEDMOETENKOMEN",
        "FIJNEAVOND",
    )
    if any(fragment in code for fragment in bogus_fragments):
        return False
    if carrier == "dhl":
        return valid_rule_tracking_code(code, "dhl")
    if carrier == "postnl":
        return valid_rule_tracking_code(code, "postnl")
    if carrier == "dpd":
        return bool(re.fullmatch(r"\d{12,16}", code))
    if carrier == "gls":
        return 8 <= len(code) <= 14
    if carrier == "fedex":
        return valid_rule_tracking_code(code, "fedex")
    if carrier == "chronopost":
        return valid_rule_tracking_code(code, "chronopost")
    if carrier in {"ups", "trunkrs", "homerr", "cycloon", "transmission", "gofo"}:
        return valid_rule_tracking_code(code, carrier)
    return 6 <= len(code) <= 40


def _extract_expected_date(value: str, today: date) -> str | None:
    lowered = value.lower()
    if re.search(r"\b(vandaag|today)\b", lowered):
        return today.isoformat()
    if re.search(r"\b(morgen|tomorrow)\b", lowered):
        return (today + timedelta(days=1)).isoformat()

    for weekday, index in WEEKDAYS.items():
        if re.search(rf"\bvoor\s+{weekday}\s+(?:zou|moet|moeten|goed)\b", lowered):
            continue
        if re.search(rf"\b(?:op|on|bezorgd op|delivered on)?\s*{weekday}\b", lowered):
            days_ahead = (index - today.weekday()) % 7
            return (today + timedelta(days=days_ahead)).isoformat()

    match = re.search(r"\b(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?\b", lowered)
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3) or today.year)
    if year < 100:
        year += 2000
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    return parsed.isoformat()


def _extract_time_window(value: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"(?i)(?:tussen|between)?\s*(\d{1,2})[:.](\d{2})\s*(?:en|and|-|tot|to)\s*(\d{1,2})[:.](\d{2})",
        value,
    )
    if not match:
        return (None, None)
    start = f"{int(match.group(1)):02d}:{match.group(2)}"
    end = f"{int(match.group(3)):02d}:{match.group(4)}"
    return (start, end)


def _extract_status(value: str, expected_date: str | None, today: date) -> str:
    lowered = value.lower()
    if re.search(r"\b(?:is|werd|was|al|succesvol)\s+(?:opgehaald|afgehaald)\b", lowered) or "picked up" in lowered:
        return STATUS_PICKED_UP
    if any(
        term in lowered
        for term in (
            "ligt klaar",
            "klaar om op te halen",
            "ready for pickup",
            "af te halen",
            "pickup ready",
            "bestelling ophalen",
            "ophalen in de apotheek",
            "uw bestelling ophalen",
        )
    ):
        return STATUS_READY_FOR_PICKUP
    if any(
        term in lowered
        for term in (
            "is bezorgd",
            "werd bezorgd",
            "pakket bezorgd",
            "bezorgd bij",
            "delivered",
            "afgeleverd:",
            "afgeleverd.",
            "is afgeleverd",
            "werd afgeleverd",
            "geleverd bij",
        )
    ):
        return STATUS_DELIVERED
    if any(
        term in lowered
        for term in (
            "out for delivery",
            "in delivery",
            "onderweg voor bezorging",
            "chauffeur is onderweg",
            "bezorger is onderweg",
            "wordt vandaag bezorgd",
            "vandaag bezorgd",
            "vandaag geleverd",
        )
    ):
        if expected_date and expected_date != today.isoformat():
            return STATUS_IN_TRANSIT
        return STATUS_EXPECTED_TODAY
    if expected_date == today.isoformat():
        return STATUS_EXPECTED_TODAY
    if expected_date:
        return STATUS_IN_TRANSIT
    if any(
        term in lowered
        for term in (
            "is onderweg",
            "onderweg",
            "in transit",
            "on the way",
            "on its way",
            "handled in our network",
            "picked up by the carrier",
            "gesorteerd",
            "sortering",
            "aangemeld",
            "shipment information received",
            "label created",
        )
    ):
        return STATUS_IN_TRANSIT
    return STATUS_UNKNOWN


def _extract_pickup_code(value: str, status: str | None = None) -> str | None:
    lowered = value.lower()
    if status not in {STATUS_READY_FOR_PICKUP, STATUS_PICKED_UP} and not any(
        term in lowered
        for term in (
            "afhaalcode",
            "ophaalcode",
            "pickup code",
            "persoonlijke code",
            "klaar om op te halen",
            "ligt klaar",
            "af te halen",
            "ophalen",
        )
    ):
        return None

    explicit = re.search(r"(?i)(?:vul|enter|gebruik|use).*?(?:code).*?[:\s]\s*([A-Z0-9]{4,12})", value)
    if explicit:
        return explicit.group(1).upper()

    patterns = (
        r"(?i)(?:afhaalcode|ophaalcode|pickup\s*code|persoonlijke\s*code|code|pincode)\s*(?:is|:)?\s*([A-Z0-9]{4,12})",
        r"(?i)\b([A-Z0-9]{4,8})\b\s*(?:is je|is uw)?\s*(?:afhaalcode|ophaalcode|pickup\s*code)",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            code = match.group(1).upper()
            if code not in {"SCAN", "CODE", "QR", "QRCODE"}:
                return code
    return None


def _extract_pickup_location(value: str) -> str | None:
    lowered = value.lower()
    if not any(
        term in lowered
        for term in (
            "ligt klaar",
            "klaar om op te halen",
            "af te halen",
            "afhaal",
            "ophalen",
            "pickup",
            "pakketpunt",
            "pakketshop",
            "parcelshop",
            "servicepoint",
            "apotheek",
            "benu",
        )
    ):
        return None

    match = re.search(r"(?i)\b(BENU\s+Apotheek[A-Za-z0-9 &'.-]{2,80})", value)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip(" .,-")[:100]

    match = re.search(r"(?i)\b(?:bij|at)\s+(?:de\s+)?([A-Z][A-Za-z0-9 &'-]{2,50})(?:[.,\n]|$)", value)
    if match:
        candidate = match.group(1).strip(" .,-")
        if candidate and not candidate.lower().startswith(("jou", "uw", "je ")):
            return candidate[:60]

    match = re.search(r"(?is)\bAdres:\s*(.+?)(?:\s+(?:Bestelgegevens|Openingstijden|Afhaalcode|Trackingnummer|Hulp nodig)\b|$)", value)
    if match:
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,-")
        if re.search(r"(?i)(straat|laan|weg|plein|dijk|kade|kamp|hof|pad|singel)\s*\d+", candidate):
            return candidate[:100]

    for hint in ("HEMA", "DHL ServicePoint", "DPD Pickup", "PostNL-punt", "pakketpunt"):
        if hint.lower() in value.lower():
            return hint
    return None


def _extract_shop(value: str, sender: str, carrier: str) -> str | None:
    lowered = value.lower()
    contextual_patterns = (
        r"(?i)\b(?:zending|pakket)\s+van\s+(.+?)\s+is\s+(?:onderweg|verzonden|aangemeld|afgeleverd|bezorgd)\b",
        r"(?i)\bafgeleverd:\s*(?:je\s+)?pakket\s+van\s+(.+?)(?:[.:\n]|$)",
        r"(?i)\bshipment\s+from\s+(.+?)\s+is\s+(?:on\s+the\s+way|in\s+transit|shipped)\b",
    )
    for pattern in contextual_patterns:
        match = re.search(pattern, value)
        if match:
            candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,-")
            if candidate and candidate.lower() not in {carrier, "postnl", "dhl", "dpd", "gls", "fedex"}:
                return candidate[:80]

    for shop, keywords in SHOP_HINTS.items():
        if any(keyword in lowered for keyword in keywords):
            return shop

    patterns = (
        r"(?i)\bafzender\s+([A-Z0-9][A-Za-z0-9 .&'-]{1,50})",
        r"(?i)\bvan:\s*([A-Z0-9][A-Za-z0-9 .&'-]{1,50})",
        r"(?i)\bfrom:\s*([A-Z0-9][A-Za-z0-9 .&'-]{1,50})",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            candidate = match.group(1).strip(" .,-")
            if candidate and candidate.lower() not in {carrier, "postnl", "dhl", "dpd", "gls", "fedex"}:
                return candidate[:60]

    sender_name = re.sub(r"<[^>]+>", "", sender).strip(" \"'")
    if sender_name and carrier not in sender_name.lower():
        return sender_name[:60]
    return None


def _excerpt(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:500]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
