"""Clean-room carrier detection and tracking-code rules."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse


CANONICAL_CARRIERS = {"postnl", "dhl", "fedex", "chronopost"}

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
        "dhlnl",
        "dhlnlpcode",
        "dhlecommerce",
        "my.dhlecommerce",
        "api-gw.dhlparcel",
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
}

URL_HOST_CARRIERS = (
    ("chronopost.fr", "chronopost"),
    ("fedex.com", "fedex"),
    ("fcbtracking.fedex.com", "fedex"),
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
    "fedex": (
        r"(?<!\d)\d{10,15}(?!\d)",
        r"(?<!\d)\d{20}(?!\d)",
        r"(?<!\d)\d{22}(?!\d)",
    ),
    "chronopost": (
        r"(?<![A-Z0-9])[A-Z]{2}\d{9}[A-Z]{2}(?![A-Z0-9])",
        r"(?<!\d)\d{13,15}(?!\d)",
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

    for carrier in ("chronopost", "fedex", "dhl", "postnl"):
        if any(alias in lowered for alias in CARRIER_ALIASES[carrier]):
            return carrier

    compact = re.sub(r"\s+", "", text.upper())
    for carrier in ("dhl", "postnl"):
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

    carriers = (canonical,) if canonical in CANONICAL_CARRIERS else ("fedex", "dhl", "postnl", "chronopost")
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
