"""Clean-room carrier detection and tracking-code rules."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse


CANONICAL_CARRIERS = {
    "postnl",
    "dhl",
    "dpd",
    "gls",
    "fedex",
    "chronopost",
    "ups",
    "trunkrs",
    "homerr",
    "cycloon",
    "instabox",
    "transmission",
    "dachser",
    "dynalogic",
    "gofo",
    "dragonfly",
}

CARRIER_ALIASES = {
    "postnl": (
        "postnl",
        "post nl",
        "mijn postnl",
        "tnt post",
        "tntp",
        "tntpit",
        "tracking.postnl",
    ),
    "dhl": (
        "dhl",
        "dhl parcel",
        "dhl ecommerce",
        "dhl netherlands",
        "dhlnl",
        "dhlnlpcode",
        "dhlecommerce",
        "my.dhlecommerce",
        "api-gw.dhlparcel",
    ),
    "dpd": (
        "dpd",
        "dpd group",
        "dpdgroup",
        "dpd.com",
    ),
    "gls": (
        "gls",
        "gls netherlands",
        "glsnl",
        "gls-info",
    ),
    "fedex": (
        "fedex",
        "fedex express",
        "fedex.com",
        "fcbtracking.fedex",
    ),
    "chronopost": (
        "chrono",
        "chronopost",
        "chronopost.fr",
    ),
    "ups": (
        "ups",
        "ups.com",
        "united parcel service",
    ),
    "trunkrs": (
        "trunkrs",
        "trnkrpcode",
        "parcel.trunkrs",
    ),
    "homerr": (
        "homerr",
        "homerr.com",
        "homerr pakketpunt",
    ),
    "cycloon": (
        "cycloon",
        "cycloon.eu",
        "cyclpcode",
    ),
    "instabox": (
        "instabox",
        "red je pakketje",
        "redjep",
    ),
    "transmission": (
        "transmission",
        "trans-mission",
        "trans mission",
        "transm",
    ),
    "dachser": (
        "dachser",
    ),
    "dynalogic": (
        "dynalogic",
        "mydynalogic",
    ),
    "gofo": (
        "gofo",
        "gofo express",
        "gofonl",
    ),
    "dragonfly": (
        "dragonfly",
        "dragonfly netherlands",
        "dragonnl",
    ),
}

URL_HOST_CARRIERS = (
    ("parcel.trunkrs.nl", "trunkrs"),
    ("trunkrs.nl", "trunkrs"),
    ("homerr.com", "homerr"),
    ("cycloon.eu", "cycloon"),
    ("instabox.io", "instabox"),
    ("redjepakketje.nl", "instabox"),
    ("trans-mission.nl", "transmission"),
    ("transmission.nl", "transmission"),
    ("dachser.com", "dachser"),
    ("dynalogic.eu", "dynalogic"),
    ("mydynalogic.eu", "dynalogic"),
    ("gofoexpress.com", "gofo"),
    ("gofo.com", "gofo"),
    ("dragonflyshipping.com", "dragonfly"),
    ("ups.com", "ups"),
    ("chronopost.fr", "chronopost"),
    ("fedex.com", "fedex"),
    ("fcbtracking.fedex.com", "fedex"),
    ("gls-info.nl", "gls"),
    ("gls-group.eu", "gls"),
    ("dpd.com", "dpd"),
    ("dhlecommerce.nl", "dhl"),
    ("dhlparcel.nl", "dhl"),
    ("dhl.com", "dhl"),
    ("postnl.nl", "postnl"),
    ("internationalparceltracking.com", "postnl"),
)

TRACKING_QUERY_KEYS = (
    "b",
    "barcode",
    "key",
    "match",
    "parcelnumber",
    "tc",
    "t",
    "tracknumber",
    "tracknumbers",
    "tracking-id",
    "tracking_id",
    "trackingnumber",
    "tracking_number",
    "trknbr",
    "listenumeroslt",
    "shipment",
    "shipmentnumber",
    "zending",
    "zendingnummer",
)

TRACKING_PATTERNS = {
    "postnl": (
        r"(?<![A-Z0-9])(?:2S|3S)[A-Z0-9]{8,20}(?![A-Z0-9])",
        r"(?<![A-Z0-9])KG[A-Z0-9]{6,12}(?![A-Z0-9])",
        r"(?<![A-Z0-9])[A-Z]{2}\d{9}NL(?![A-Z0-9])",
    ),
    "dhl": (
        r"(?<![A-Z0-9])(?:JJD|JD|JVGL)[A-Z0-9]{10,30}(?![A-Z0-9])",
        r"(?<![A-Z0-9])3S[A-Z0-9]{8,20}(?![A-Z0-9])",
    ),
    "dpd": (
        r"(?<!\d)\d{12,16}(?!\d)",
    ),
    "gls": (
        r"(?<![A-Z0-9])[A-Z0-9]{8,14}(?![A-Z0-9])",
    ),
    "fedex": (
        r"(?<!\d)\d{10,15}(?!\d)",
        r"(?<!\d)\d{20}(?!\d)",
        r"(?<!\d)\d{22}(?!\d)",
    ),
    "chronopost": (
        r"(?<![A-Z0-9])[A-Z]{2}\d{9}[A-Z]{2}(?![A-Z0-9])",
        r"(?<!\d)\d{13,15}(?!\d)",
    ),
    "ups": (
        r"(?<![A-Z0-9])1Z[A-Z0-9]{16}(?![A-Z0-9])",
    ),
    "trunkrs": (
        r"(?<!\d)4\d{7,15}(?!\d)",
    ),
    "homerr": (
        r"(?<![A-Z0-9])HMR[A-Z0-9]{14}(?![A-Z0-9])",
    ),
    "cycloon": (
        r"(?<![A-Z0-9])FKS[A-Z0-9]{6,24}(?![A-Z0-9])",
    ),
    "transmission": (
        r"(?<![A-Z0-9])T[A-Z0-9]{14}(?![A-Z0-9])",
    ),
    "gofo": (
        r"(?<![A-Z0-9])GF\d{10,20}(?![A-Z0-9])",
    ),
}


def clean_rule_text(value: str | None) -> str:
    """Normalize enough for carrier rules without importing parser.py."""
    if not value:
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_carrier(value: str | None) -> str:
    """Map known app/provider carrier ids to our canonical carrier slugs."""
    text = clean_rule_text(value).lower()
    if not text:
        return "unknown"
    if "post" in text and "nl" in text:
        return "postnl"
    if text in {"tntp", "tntpit", "tntpitp"}:
        return "postnl"
    for carrier, aliases in CARRIER_ALIASES.items():
        if carrier == text or any(alias in text for alias in aliases):
            return carrier
    return text[:32]


def detect_carrier(value: str | None) -> str:
    """Detect FedEx/DHL/PostNL from URLs, sender text, or strong code rules."""
    text = clean_rule_text(value)
    lowered = text.lower()

    for url in extract_urls(text):
        host = urlparse(url).netloc.lower()
        for host_fragment, carrier in URL_HOST_CARRIERS:
            if host_fragment in host:
                return carrier

    for carrier in CARRIER_ALIASES:
        if any(alias in lowered for alias in CARRIER_ALIASES[carrier]):
            return carrier

    compact = re.sub(r"\s+", "", text.upper())
    for carrier in CANONICAL_CARRIERS:
        if _first_matching_code(compact, carrier):
            return carrier
    return "unknown"


def extract_tracking_code(value: str | None, carrier: str | None = None) -> str | None:
    """Extract a canonical tracking code for a carrier."""
    text = clean_rule_text(value)
    canonical = normalize_carrier(carrier)
    url_code = extract_tracking_code_from_url(text, canonical)
    if url_code and valid_tracking_code(url_code, canonical):
        return url_code

    carriers = (canonical,) if canonical in CANONICAL_CARRIERS else tuple(TRACKING_PATTERNS)
    for candidate_carrier in carriers:
        for source in (text, re.sub(r"\s+", "", text.upper())):
            match = _first_matching_code(source, candidate_carrier)
            if match:
                return match
    return None


def extract_tracking_code_from_url(value: str | None, carrier: str | None = None) -> str | None:
    """Extract common carrier tracking query parameters from URLs."""
    canonical = normalize_carrier(carrier)
    for url in extract_urls(value or ""):
        parsed = urlparse(url)
        query = {key.lower(): items for key, items in parse_qs(parsed.query).items()}
        for key in TRACKING_QUERY_KEYS:
            for item in query.get(key, []):
                code = re.sub(r"[^A-Z0-9]", "", unquote(item).upper())
                if 6 <= len(code) <= 40 and valid_tracking_code(code, canonical):
                    return code
        path_code = re.sub(r"[^A-Z0-9]", "", unquote(parsed.path).upper())
        if canonical in CANONICAL_CARRIERS and valid_tracking_code(path_code, canonical):
            return path_code
    return None


def valid_tracking_code(code: str | None, carrier: str | None = None) -> bool:
    """Return whether a code is plausible for the given carrier."""
    code = re.sub(r"[^A-Z0-9]", "", str(code or "").upper())
    canonical = normalize_carrier(carrier)
    if not code or not any(char.isdigit() for char in code):
        return False
    if canonical == "postnl":
        return any(re.fullmatch(pattern, code) for pattern in TRACKING_PATTERNS["postnl"])
    if canonical == "dhl":
        return any(re.fullmatch(pattern, code) for pattern in TRACKING_PATTERNS["dhl"])
    if canonical == "fedex":
        return code.isdigit() and (10 <= len(code) <= 15 or len(code) in {20, 22})
    if canonical == "chronopost":
        return any(re.fullmatch(pattern, code) for pattern in TRACKING_PATTERNS["chronopost"])
    if canonical in TRACKING_PATTERNS:
        return any(re.fullmatch(pattern, code) for pattern in TRACKING_PATTERNS[canonical])
    return 6 <= len(code) <= 40


def extract_urls(value: str | None) -> list[str]:
    """Return http(s) URLs from text."""
    decoded = html.unescape(value or "")
    return [match.group(0).rstrip(").,;]") for match in re.finditer(r"https?://[^\s\"'<>]+", decoded)]


def _first_matching_code(value: str, carrier: str) -> str | None:
    for pattern in TRACKING_PATTERNS.get(carrier, ()):
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            code = re.sub(r"[^A-Z0-9]", "", match.group(0).upper())
            if valid_tracking_code(code, carrier):
                return code
    return None
