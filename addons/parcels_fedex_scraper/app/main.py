"""Local tracking scraper sidecar for personal parcels-hass setups."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import logging
import os
from pathlib import Path
import re
import unicodedata
from typing import Any
from urllib.parse import quote_plus

from aiohttp import web
from playwright.async_api import Browser, Error as PlaywrightError, async_playwright

LOG = logging.getLogger("parcels_fedex_scraper")

ADDON_OPTIONS_PATH = Path("/data/options.json")
SERVICE_NAME = "parcels-tracking-scraper"
VINTED_PROFILE_DIR = Path("/data/browser-profiles/vinted")
FEDEX_HOME = "https://www.fedex.com/en-us/home.html"
FEDEX_TRACKING_PAGE = "https://www.fedex.com/fedextrack/?trknbr={tracking_code}"
CHRONOPOST_TRACKING_PAGE = "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT={tracking_code}"
VINTED_HOME = "https://www.vinted.nl/"
VINTED_LOGIN_URL = "https://www.vinted.nl/member/general/login"
SUPPORTED_CARRIERS = {"fedex", "chronopost"}
CAPTURE_URL_HINTS = (
    "trackingcal",
    "api.fedex.com/track/",
    "track/v2/shipments",
    "track/v1/trackingnumbers",
)
BLOCKED_HINTS = (
    "permission to view this webpage",
    "can't process your request",
    "cannot process your request",
    "system down",
    "access denied",
    "captcha",
    "cloudflare",
    "just a moment",
    "enable javascript and cookies",
)
CHRONOPOST_CAPTURE_HINTS = (
    "chronopost",
    "suivi",
    "tracking",
    "idship",
    "shipment",
    "track",
)
CHRONOPOST_PICKUP_HINTS = (
    "pick up point",
    "point relais",
    "relais pickup",
    "point pickup",
    "point de retrait",
    "bureau de poste",
    "commerce",
    "commercant",
    "afhaalpunt",
    "pickup point",
    "collection point",
    "mis a disposition",
    "a retirer",
    "disponible au point",
)
PLACEHOLDER_PICKUP_LOCATIONS = {
    "normal",
    "pickup",
    "pick up point",
    "point relais",
    "relais point",
    "chronopost relais point",
    "chronopost relay point",
}


@dataclass(slots=True)
class VintedAccount:
    key: str
    email: str
    password: str
    profile_dir: Path


@dataclass(slots=True)
class Settings:
    host: str
    port: int
    token: str
    headless: bool
    timeout: int
    vinted_auto_login: bool
    vinted_accounts: tuple[VintedAccount, ...]
    vinted_login_interval_hours: int
    vinted_login_on_start: bool


def settings_from_env() -> Settings:
    return settings_from_options(load_addon_options())


def settings_from_options(addon_options: dict[str, Any]) -> Settings:
    return Settings(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=parse_int(os.environ.get("PORT"), parse_int(addon_options.get("port"), 8765)),
        token=os.environ.get("SCRAPER_TOKEN") or str(addon_options.get("scraper_token") or ""),
        headless=parse_bool(
            addon_options.get("headless"),
            parse_bool(os.environ.get("FEDEX_SCRAPER_HEADLESS"), True),
        ),
        timeout=parse_int(
            addon_options.get("timeout"),
            parse_int(os.environ.get("FEDEX_SCRAPER_TIMEOUT"), 45),
        ),
        vinted_auto_login=parse_bool(addon_options.get("vinted_auto_login"), False),
        vinted_accounts=vinted_accounts_from_options(addon_options),
        vinted_login_interval_hours=parse_int(addon_options.get("vinted_login_interval_hours"), 22),
        vinted_login_on_start=parse_bool(addon_options.get("vinted_login_on_start"), True),
    )


def vinted_accounts_from_options(addon_options: dict[str, Any]) -> tuple[VintedAccount, ...]:
    raw_accounts: list[tuple[str, str, str]] = []
    for key, email_option, password_option, email_env, password_env in (
        ("account_1", "vinted_email", "vinted_password", "VINTED_EMAIL", "VINTED_PASSWORD"),
        ("account_2", "vinted_email_2", "vinted_password_2", "VINTED_EMAIL_2", "VINTED_PASSWORD_2"),
    ):
        email = str(addon_options.get(email_option) or os.environ.get(email_env) or "").strip()
        password = str(addon_options.get(password_option) or os.environ.get(password_env) or "")
        if email and password:
            raw_accounts.append((key, email, password))

    accounts: list[VintedAccount] = []
    use_legacy_single_profile = len(raw_accounts) == 1 and raw_accounts[0][0] == "account_1"
    for key, email, password in raw_accounts:
        profile_dir = VINTED_PROFILE_DIR if use_legacy_single_profile else VINTED_PROFILE_DIR / key
        accounts.append(VintedAccount(key=key, email=email, password=password, profile_dir=profile_dir))
    return tuple(accounts)


def load_addon_options(path: Path = ADDON_OPTIONS_PATH) -> dict[str, Any]:
    """Read Home Assistant add-on options when running under Supervisor."""

    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as err:
        LOG.warning("Could not read Home Assistant add-on options from %s: %s", path, err)
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def parse_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    token = request.app["settings"].token
    if not token:
        return await handler(request)
    auth = request.headers.get("Authorization", "")
    alt = request.headers.get("X-Parcels-Scraper-Token", "")
    if auth == f"Bearer {token}" or alt == token:
        return await handler(request)
    raise web.HTTPUnauthorized(text="missing or invalid scraper token")


async def health(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    return web.json_response(
        {
            "ok": True,
            "service": SERVICE_NAME,
            "started_at": request.app.get("started_at"),
            "headless": settings.headless,
            "timeout": settings.timeout,
            "vinted": vinted_status_payload(request.app),
        }
    )


async def vinted_login_status(request: web.Request) -> web.Response:
    return web.json_response(vinted_status_payload(request.app))


async def vinted_login_refresh(request: web.Request) -> web.Response:
    account_key = None
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        account_key = str(payload.get("account") or payload.get("account_key") or "").strip() or None
    result = await refresh_vinted_login(request.app, reason="manual", account_key=account_key)
    return web.json_response(result)


async def track(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception as err:
        raise web.HTTPBadRequest(text="invalid json") from err

    carrier = str(payload.get("carrier") or "fedex").strip().lower()
    tracking_code = normalize_tracking_code(payload.get("tracking_code"))
    if carrier not in SUPPORTED_CARRIERS:
        raise web.HTTPBadRequest(text=f"unsupported carrier: {carrier}")
    if not tracking_code:
        raise web.HTTPBadRequest(text="missing tracking_code")

    tracking_url = str(payload.get("tracking_url") or "").strip() or default_tracking_url(carrier, tracking_code)
    timeout = request.app["settings"].timeout
    try:
        browser = await get_shared_browser(request.app)
        if carrier == "chronopost":
            scrape = scrape_chronopost(
                browser,
                tracking_code=tracking_code,
                tracking_url=tracking_url,
                timeout=timeout,
            )
        else:
            scrape = scrape_fedex(
                browser,
                tracking_code=tracking_code,
                tracking_url=tracking_url,
                timeout=timeout,
            )
        result = await asyncio.wait_for(
            scrape,
            timeout=timeout,
        )
    except TimeoutError:
        LOG.info(
            "%s scrape timed out after %ss for %s",
            carrier_label(carrier),
            timeout,
            redact_tracking_code(tracking_code),
        )
        result = error_update(tracking_code, tracking_url, f"scraper_timeout_{timeout}s", carrier=carrier)
    except PlaywrightError as err:
        LOG.warning("%s browser failed for %s: %s", carrier_label(carrier), redact_tracking_code(tracking_code), err)
        result = error_update(tracking_code, tracking_url, "browser_error", carrier=carrier)
    LOG.info(
        "%s tracking %s -> status=%s error=%s source=%s",
        carrier_label(carrier),
        redact_tracking_code(tracking_code),
        result.get("status"),
        result.get("tracking_refresh_error") or "-",
        result.get("tracking_refresh_source") or "-",
    )
    return web.json_response(result)


def default_tracking_url(carrier: str, tracking_code: str) -> str:
    template = CHRONOPOST_TRACKING_PAGE if carrier == "chronopost" else FEDEX_TRACKING_PAGE
    return template.format(tracking_code=quote_plus(tracking_code))


def carrier_label(carrier: str) -> str:
    return "Chronopost" if carrier == "chronopost" else "FedEx"


def normalize_tracking_code(value: Any) -> str:
    code = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return code if 10 <= len(code) <= 40 else ""


def redact_tracking_code(value: str) -> str:
    return f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "CODE"


async def scrape_fedex(
    browser: Browser,
    *,
    tracking_code: str,
    tracking_url: str,
    timeout: int,
) -> dict[str, Any]:
    context = await browser.new_context(
        locale="nl-NL",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
    )
    page = await context.new_page()
    loop = asyncio.get_running_loop()
    api_payload: asyncio.Future[tuple[str, Any]] = loop.create_future()

    async def capture_response(response) -> None:
        if api_payload.done():
            return
        url = response.url.lower()
        if not any(hint in url for hint in CAPTURE_URL_HINTS):
            return
        try:
            payload = await response.json()
        except Exception as err:
            LOG.debug("FedEx response was not JSON from %s: %s", response.url, err)
            return
        if not api_payload.done():
            api_payload.set_result((response.url, payload))

    page.on("response", lambda response: asyncio.create_task(capture_response(response)))

    try:
        await page.goto(FEDEX_HOME, wait_until="domcontentloaded", timeout=timeout * 1000)
        await page.goto(tracking_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        try:
            source_url, payload = await asyncio.wait_for(api_payload, timeout=timeout)
            update = normalize_fedex_json(payload, tracking_code=tracking_code, source_url=source_url)
            update["tracking_url"] = tracking_url
            return update
        except TimeoutError:
            LOG.info("FedEx JSON capture timed out for %s; falling back to page text", redact_tracking_code(tracking_code))
            content = await page.content()
            update = normalize_fedex_html(content, tracking_code=tracking_code, tracking_url=tracking_url)
            return update
    except PlaywrightError as err:
        LOG.warning("FedEx Playwright error for %s: %s", redact_tracking_code(tracking_code), err)
        return error_update(tracking_code, tracking_url, f"playwright_error: {err}")
    finally:
        await context.close()


async def scrape_chronopost(
    browser: Browser,
    *,
    tracking_code: str,
    tracking_url: str,
    timeout: int,
) -> dict[str, Any]:
    context = await browser.new_context(
        locale="nl-NL",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
    )
    page = await context.new_page()
    json_payloads: list[tuple[str, Any]] = []

    async def capture_response(response) -> None:
        url = response.url.lower()
        if not any(hint in url for hint in CHRONOPOST_CAPTURE_HINTS):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        json_payloads.append((response.url, payload))

    page.on("response", lambda response: asyncio.create_task(capture_response(response)))

    try:
        await page.goto(tracking_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        await dismiss_cookie_banner(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=min(timeout * 1000, 15000))
        except PlaywrightError:
            pass
        await page.wait_for_timeout(min(5000, max(1000, timeout * 250)))
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(750)

        page_text = await page.locator("body").inner_text(timeout=5000)
        page_update = normalize_chronopost_text(page_text, tracking_code=tracking_code, tracking_url=tracking_url)
        if has_delivery_detail(page_update):
            return page_update

        for source_url, payload in reversed(json_payloads):
            update = normalize_chronopost_json(
                payload,
                tracking_code=tracking_code,
                tracking_url=tracking_url,
                source_url=source_url,
            )
            if has_delivery_detail(update):
                return update

        return page_update
    except PlaywrightError as err:
        LOG.warning("Chronopost Playwright error for %s: %s", redact_tracking_code(tracking_code), err)
        return error_update(tracking_code, tracking_url, f"playwright_error: {err}", carrier="chronopost")
    finally:
        await context.close()


async def dismiss_cookie_banner(page) -> None:
    for label in ("Tout accepter", "Accepter", "Accept all", "Accept"):
        try:
            await page.get_by_role("button", name=re.compile(label, re.IGNORECASE)).click(timeout=1000)
            return
        except PlaywrightError:
            continue


def normalize_chronopost_json(
    payload: Any,
    *,
    tracking_code: str,
    tracking_url: str,
    source_url: str,
) -> dict[str, Any]:
    """Normalize Chronopost JSON-ish browser responses by reusing the text parser."""

    text = "\n".join(flatten_strings(payload))
    update = normalize_chronopost_text(text, tracking_code=tracking_code, tracking_url=tracking_url)
    update["tracking_api_url"] = source_url
    return update


def normalize_chronopost_text(
    page_text: str,
    *,
    tracking_code: str,
    tracking_url: str,
) -> dict[str, Any]:
    lines = meaningful_lines(page_text)
    text = "\n".join(lines)
    folded = fold_text(text)
    if any(hint in folded for hint in BLOCKED_HINTS):
        return error_update(tracking_code, tracking_url, "chronopost_page_blocked_or_permission", carrier="chronopost")

    latest_status, latest_text = chronopost_latest_status(lines)
    status = latest_status or map_chronopost_status(folded)
    pickup_location = chronopost_pickup_location(lines) if status == "ready_for_pickup" else ""
    status_text = latest_text or chronopost_status_text(lines, status=status, pickup_location=pickup_location)
    expected = "" if latest_status in {"in_transit", "delivered"} else chronopost_date(text)
    start, end = chronopost_window(text)

    update = {
        "carrier": "chronopost",
        "tracking_code": tracking_code,
        "tracking_url": tracking_url,
        "status": status,
        "raw_status": status_text,
        "tracking_status_text": status_text or text[:220],
        "tracking_refresh_source": "local_tracking_scraper",
        "tracking_refresh_supported": True,
    }
    if pickup_location:
        update["pickup_location"] = pickup_location
        update["status"] = "ready_for_pickup"
        update["tracking_status_text"] = f"Afhalen bij {pickup_location}"[:220]
        return update
    if expected:
        update["expected_date"] = expected
    if start and end:
        update["delivery_window_start"] = start
        update["delivery_window_end"] = end
    if status == "unknown" and not expected and not (start and end):
        update["tracking_refresh_error"] = "chronopost_no_delivery_detail"
    return update


def chronopost_latest_status(lines: list[str]) -> tuple[str, str]:
    """Return status from the newest visible Chronopost event.

    Chronopost shows newest events first. Older setup fields can contain
    "Pick up point", but those are not the current parcel state.
    """

    for index, line in enumerate(lines):
        folded = fold_text(line)
        if "pick up point :" in folded:
            break
        if is_chronopost_status_noise(line):
            continue
        status = chronopost_status_from_event_text(folded)
        if status:
            location = chronopost_event_location(lines, index)
            text = f"{line} - {location}" if location else line
            return status, text[:220]
    return "", ""


def chronopost_event_location(lines: list[str], status_index: int) -> str:
    for candidate in reversed(lines[max(0, status_index - 4) : status_index]):
        if is_chronopost_status_noise(candidate):
            continue
        folded = fold_text(candidate)
        if chronopost_status_from_event_text(folded):
            continue
        if "chronopost" in folded or re.search(r"\b[A-Z]{2,}\b", candidate):
            return candidate[:120]
    return ""


def chronopost_status_from_event_text(folded: str) -> str:
    if any(
        term in folded
        for term in (
            "shipment in transit",
            "outbound linehaul scan",
            "sorted at departure location",
            "parcel collected by carrier",
            "parcel handed over from pickup point to the driver",
            "shipment handed over by shipper",
            "shipment in preparation to be shipped",
            "sending supported by chronopost, in transit",
            "acheminement",
            "pris en charge",
            "centre de tri",
            "transit",
        )
    ):
        return "in_transit"
    if any(
        term in folded
        for term in (
            "disponible au point",
            "mis a disposition",
            "a retirer",
            "ready for pickup",
            "available at pickup point",
            "available at pick up point",
            "delivered at pickup point",
            "delivered to pickup point",
            "livre au point relais",
            "livre en point relais",
            "livre dans un point relais",
            "livre a un point relais",
        )
    ):
        return "ready_for_pickup"
    if any(term in folded for term in ("out for delivery", "en cours de livraison", "sera livre aujourd")):
        return "expected_today"
    if any(term in folded for term in ("delivered", "remis au destinataire", "colis remis", "colis livre")):
        return "delivered"
    return ""


def is_chronopost_status_noise(line: str) -> bool:
    folded = fold_text(line)
    if not folded:
        return True
    if re.match(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", folded):
        return True
    if re.match(r"^(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b", folded):
        return True
    if re.match(r"^\d{1,2}:\d{2}\s*(?:am|pm)?$", folded):
        return True
    if re.match(r"^\d{1,2}[/-]\d{1,2}[/-]20\d{2}\b", folded):
        return True
    return any(
        term in folded
        for term in (
            "estimated delivery date",
            "subscribe to my parcel tracking",
            "some tips",
            "the steps of my delivery",
            "type of collection point",
            "partner number",
        )
    )


def has_delivery_detail(update: dict[str, Any] | None) -> bool:
    if not update or update.get("tracking_refresh_error"):
        return False
    if update.get("pickup_location") and not is_placeholder_pickup_location(str(update.get("pickup_location"))):
        return True
    if update.get("expected_date"):
        return True
    if update.get("delivery_window_start") and update.get("delivery_window_end"):
        return True
    return update.get("status") not in {None, "", "unknown"}


def map_chronopost_status(text: str, *, has_pickup_location: bool = False) -> str:
    if any(
        term in text
        for term in (
            "disponible au point",
            "mis a disposition",
            "a retirer",
            "ready for pickup",
            "available at pickup point",
            "available at pick up point",
            "delivered at pickup point",
            "delivered to pickup point",
            "livre au point relais",
            "livre en point relais",
            "livre dans un point relais",
            "livre a un point relais",
        )
    ):
        return "ready_for_pickup"
    if any(term in text for term in ("delivered", "remis au destinataire", "colis remis", "colis livre")):
        return "delivered"
    if any(term in text for term in ("en cours de livraison", "livraison prevue aujourd", "sera livre aujourd")):
        return "expected_today"
    if any(term in text for term in ("acheminement", "pris en charge", "centre de tri", "arrive", "transit")):
        return "in_transit"
    return "unknown"


def chronopost_status_text(lines: list[str], *, status: str, pickup_location: str) -> str:
    if pickup_location:
        return f"Afhalen bij {pickup_location}"
    folded_terms = {
        "expected_today": ("en cours de livraison", "livraison prevue", "sera livre"),
        "in_transit": ("acheminement", "pris en charge", "centre de tri", "arrive", "transit"),
        "delivered": ("livre", "remis au destinataire"),
        "ready_for_pickup": CHRONOPOST_PICKUP_HINTS,
    }.get(status, ())
    for line in lines:
        folded = fold_text(line)
        if any(term in folded for term in folded_terms):
            return line[:220]
    return (lines[0] if lines else "")[:220]


def chronopost_pickup_location(lines: list[str]) -> str:
    explicit = explicit_chronopost_pickup_location(lines)
    if explicit:
        return explicit

    for index, line in enumerate(lines):
        folded = fold_text(line)
        if is_non_destination_pickup_line(line):
            continue
        if not any(hint in folded for hint in CHRONOPOST_PICKUP_HINTS):
            continue
        if not is_destination_pickup_hint_line(line):
            continue
        pieces: list[str] = []
        after = text_after_pickup_hint(line)
        if is_location_piece(after):
            pieces.append(after)
        for extra in lines[index + 1 : index + 8]:
            if len(pieces) >= 4:
                break
            if is_location_stop_line(extra, have_location=bool(pieces)):
                if pieces:
                    break
                continue
            if is_location_piece(extra):
                pieces.append(extra)
        location = format_location(pieces)
        if location:
            return location
    return ""


def is_destination_pickup_hint_line(line: str) -> bool:
    folded = fold_text(line)
    return any(
        term in folded
        for term in (
            "disponible au point",
            "mis a disposition",
            "a retirer",
            "ready for pickup",
            "available at pickup point",
            "available at pick up point",
            "delivered at pickup point",
            "delivered to pickup point",
            "livre au point relais",
            "livre en point relais",
            "livre dans un point relais",
            "livre a un point relais",
            "sera livre dans le point relais",
            "sera livre dans le point pickup",
            "livre dans le point relais",
            "livre dans le point pickup",
        )
    )


def explicit_chronopost_pickup_location(lines: list[str]) -> str:
    """Extract Chronopost's destination pickup field.

    Chronopost renders this as:
    Pick up point : SHOP - STREET 1 - 1234 AB - CITY - NL
    The browser text can wrap after a dash, so a couple of continuation lines
    are folded in before parsing.
    """

    for index, line in enumerate(lines):
        match = re.search(r"\bpick\s*up\s*point\s*:\s*(.+)", line, re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1).strip()
        for extra in lines[index + 1 : index + 4]:
            if not looks_like_pickup_point_continuation(candidate, extra):
                break
            candidate = f"{candidate} {extra}".strip()
        location = format_explicit_pickup_point(candidate)
        if location:
            return location
    return ""


def looks_like_pickup_point_continuation(candidate: str, extra: str) -> bool:
    folded = fold_text(extra)
    if not extra or is_location_stop_line(extra, have_location=True):
        return False
    if any(term in folded for term in ("i wish", "subscribe", "general condition", "validate", "frequently asked")):
        return False
    if candidate.rstrip().endswith("-"):
        return True
    if re.search(r"\b\d{4}\s?[A-Z]{2}\b", extra):
        return True
    return bool(re.search(r"\s-\s", extra) and len(extra) <= 120)


def format_explicit_pickup_point(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -:;,")
    text = re.sub(r"\s+-\s+", " - ", text)
    text = re.sub(r"-\s+", "- ", text)
    text = re.sub(r"\s+-", " -", text)
    parts = [clean_location_piece(part) for part in re.split(r"\s+-\s+", text)]
    parts = [part for part in parts if part]
    if len(parts) >= 3:
        return " - ".join(parts[:5])[:180]
    return ""


def is_non_destination_pickup_line(line: str) -> bool:
    folded = fold_text(line)
    return any(
        term in folded
        for term in (
            "handed over from pickup point to the driver",
            "from pickup point to the driver",
            "type of collection point",
            "collection point type",
            "pickup point type",
            "parcel handed over from pickup point",
        )
    )


def text_after_pickup_hint(line: str) -> str:
    folded = fold_text(line)
    best_pos = -1
    best_hint = ""
    for hint in CHRONOPOST_PICKUP_HINTS:
        pos = folded.find(hint)
        if pos >= 0 and (best_pos < 0 or pos < best_pos):
            best_pos = pos
            best_hint = hint
    if best_pos < 0:
        return ""
    raw_after = line[best_pos + len(best_hint) :]
    raw_after = re.sub(r"^[\s:,\-.]+", "", raw_after)
    raw_after = re.sub(r"^(pickup|relais|point)\b[\s:,\-.]*", "", raw_after, flags=re.IGNORECASE)
    return raw_after.strip()


def is_location_piece(value: str) -> bool:
    text = clean_location_piece(value)
    if len(text) < 3 or len(text) > 120:
        return False
    folded = fold_text(text)
    if re.match(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", folded):
        return False
    if re.match(r"^(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b", folded):
        return False
    if re.match(r"^\d{1,2}[/-]\d{1,2}[/-]20\d{2}\b", folded):
        return False
    if folded.startswith("type :") or folded.startswith("type:"):
        return False
    if is_placeholder_pickup_location(text):
        return False
    if is_location_stop_line(text, have_location=False):
        return False
    if any(term in folded for term in ("colis", "livraison", "suivre", "historique", "connexion", "loading")):
        return False
    return bool(re.search(r"\d", text) or re.search(r"\b[A-Z0-9][A-Z0-9&' -]{2,}\b", text))


def is_location_stop_line(value: str, *, have_location: bool) -> bool:
    folded = fold_text(value)
    stop_terms = (
        "historique",
        "suivre",
        "suivez",
        "numero",
        "n de colis",
        "etape",
        "date",
        "contact",
        "se connecter",
        "loading",
        "chronopost",
    )
    if any(term in folded for term in stop_terms):
        return True
    if have_location and any(term in folded for term in ("colis", "livraison", "expediteur", "destinataire")):
        return True
    return False


def format_location(pieces: list[str]) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        text = clean_location_piece(piece)
        if not text:
            continue
        key = fold_text(text)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return ", ".join(cleaned)[:180]


def is_placeholder_pickup_location(value: str) -> bool:
    return fold_text(value) in PLACEHOLDER_PICKUP_LOCATIONS


def clean_location_piece(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ,.;:-")
    text = re.sub(r"\s+le\s+\d{1,2}\s+[A-Za-z]+.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(aujourd'hui|demain).*$", "", text, flags=re.IGNORECASE)
    return text.strip(" ,.;:-")


def chronopost_date(value: str) -> str:
    iso = date_from_any(value)
    if iso:
        return iso
    folded = fold_text(value)
    months = {
        "janvier": 1,
        "fevrier": 2,
        "mars": 3,
        "avril": 4,
        "mai": 5,
        "juin": 6,
        "juillet": 7,
        "aout": 8,
        "septembre": 9,
        "octobre": 10,
        "novembre": 11,
        "decembre": 12,
    }
    match = re.search(
        r"\b(\d{1,2})\s+("
        + "|".join(months)
        + r")(?:\s+(20\d{2}))?\b",
        folded,
    )
    if not match:
        return ""
    year = int(match.group(3) or datetime.now().year)
    try:
        return datetime(year, months[match.group(2)], int(match.group(1))).date().isoformat()
    except ValueError:
        return ""


def chronopost_window(value: str) -> tuple[str, str]:
    match = re.search(r"\b([0-2]?\d[:.h][0-5]\d)\s*(?:-|a|et|/)\s*([0-2]?\d[:.h][0-5]\d)\b", fold_text(value))
    if not match:
        return ("", "")
    start = time_from_any(match.group(1))
    end = time_from_any(match.group(2))
    return (start, end) if start and end and start != end else ("", "")


def meaningful_lines(value: str) -> list[str]:
    raw_lines = re.split(r"[\r\n]+", str(value or ""))
    if len(raw_lines) <= 1:
        raw_lines = re.split(r"\s{2,}", str(value or ""))
    lines: list[str] = []
    seen: set[str] = set()
    for raw in raw_lines:
        line = re.sub(r"\s+", " ", html.unescape(raw)).strip()
        if not line:
            continue
        key = fold_text(line)
        if key in seen:
            continue
        seen.add(key)
        if key in {"x", "menu", "ok", "fr", "nl", "en"}:
            continue
        lines.append(line)
    return lines[:120]


def flatten_strings(value: Any, *, limit: int = 300) -> list[str]:
    results: list[str] = []

    def walk(item: Any) -> None:
        if len(results) >= limit:
            return
        if isinstance(item, dict):
            for nested in item.values():
                walk(nested)
            return
        if isinstance(item, list):
            for nested in item:
                walk(nested)
            return
        if item is None or isinstance(item, (bool, int, float)):
            return
        text = re.sub(r"\s+", " ", str(item)).strip()
        if len(text) >= 2:
            results.append(text)

    walk(value)
    return results


def fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", ascii_text).strip().lower()


def normalize_fedex_json(payload: Any, *, tracking_code: str, source_url: str) -> dict[str, Any]:
    package = first_fedex_package(payload)
    if not isinstance(package, dict):
        return error_update(tracking_code, None, "fedex_json_no_package")

    raw_status = first_text(
        package.get("keyStatus"),
        package.get("mainStatus"),
        nested_value(package, ("latestStatusDetail", "statusByLocale")),
        nested_value(package, ("latestStatusDetail", "description")),
        package.get("statusWithDetails"),
    )
    code = first_text(
        package.get("keyStatusCD"),
        nested_value(package, ("latestStatusDetail", "code")),
    )
    status = map_status(f"{code} {raw_status}")
    location = fedex_location(package)
    expected = fedex_date(package, preferred_types=("ACTUAL_DELIVERY", "ESTIMATED_DELIVERY"))
    start, end = fedex_window(package)
    events = fedex_events(package)

    parts = [raw_status]
    details = first_text(package.get("statusWithDetails"), package.get("subStatus"))
    if details and details.lower() not in raw_status.lower():
        parts.append(details)
    if location:
        parts.append(location)

    update = {
        "carrier": "fedex",
        "tracking_code": str(package.get("trackingNbr") or tracking_code),
        "status": status,
        "raw_status": raw_status,
        "tracking_status_text": " - ".join(part for part in parts if part)[:220],
        "tracking_refresh_source": "local_tracking_scraper",
        "tracking_refresh_supported": True,
        "tracking_api_url": source_url,
        "events": events,
    }
    if expected:
        update["expected_date"] = expected
    if start and end:
        update["delivery_window_start"] = start
        update["delivery_window_end"] = end
    if location:
        update["location"] = location
    return update


def normalize_fedex_html(html_text: str, *, tracking_code: str, tracking_url: str) -> dict[str, Any]:
    text = html_to_text(html_text)
    lowered = text.lower()
    if any(hint in lowered for hint in BLOCKED_HINTS):
        return error_update(tracking_code, tracking_url, "fedex_page_blocked_or_permission")

    status_match = re.search(
        r"\b(Delivered|Out for delivery|On FedEx vehicle for delivery|In transit|On the way|Delivery exception)\b",
        text,
        re.IGNORECASE,
    )
    raw_status = status_match.group(1) if status_match else ""
    return {
        "carrier": "fedex",
        "tracking_code": tracking_code,
        "tracking_url": tracking_url,
        "status": map_status(raw_status),
        "raw_status": raw_status,
        "tracking_status_text": raw_status or text[:220],
        "tracking_refresh_source": "local_tracking_scraper",
        "tracking_refresh_supported": True,
    }


def first_fedex_package(payload: Any) -> Any:
    if isinstance(payload, dict):
        packages = nested_value(payload, ("output", "packages"))
        if isinstance(packages, list) and packages:
            return packages[0]
        complete = nested_value(payload, ("output", "completeTrackResults"))
        if isinstance(complete, list) and complete:
            results = complete[0].get("trackResults")
            if isinstance(results, list) and results:
                return results[0]
        if any(key in payload for key in ("trackingNbr", "latestStatusDetail", "keyStatus")):
            return payload
    return None


def nested_value(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_text(*values: Any) -> str:
    for value in values:
        if value:
            text = re.sub(r"\s+", " ", str(value)).strip()
            if text:
                return text
    return ""


def fedex_location(package: dict[str, Any]) -> str:
    sources = (
        package.get("statusLocationAddress"),
        nested_value(package, ("lastUpdatedDestinationAddress",)),
    )
    for source in sources:
        if not isinstance(source, dict):
            continue
        parts = [source.get(key) for key in ("city", "stateOrProvinceCode", "countryCode") if source.get(key)]
        if parts:
            return ", ".join(str(part) for part in parts)
    events = package.get("scanEventList") or package.get("scanEvents")
    if isinstance(events, list) and events:
        location = events[-1].get("scanLocation") or events[-1].get("scanLocationAddress")
        if isinstance(location, dict):
            parts = [location.get(key) for key in ("city", "stateOrProvinceCode", "countryCode") if location.get(key)]
            return ", ".join(str(part) for part in parts)
        if location:
            return str(location)
    return ""


def fedex_date(package: dict[str, Any], *, preferred_types: tuple[str, ...]) -> str:
    for key in ("actDeliveryDt", "estDeliveryDt", "displayActDeliveryDt", "displayEstDeliveryDt"):
        parsed = date_from_any(package.get(key))
        if parsed:
            return parsed
    date_times = package.get("dateAndTimes")
    if isinstance(date_times, list):
        by_type = {str(item.get("type") or "").upper(): item.get("dateTime") for item in date_times if isinstance(item, dict)}
        for date_type in preferred_types:
            parsed = date_from_any(by_type.get(date_type))
            if parsed:
                return parsed
    return ""


def fedex_window(package: dict[str, Any]) -> tuple[str, str]:
    window = package.get("estDelTimeWindow")
    if isinstance(window, dict):
        for start_key, end_key in (
            ("displayEstDelTmWindowTmStart", "displayEstDelTmWindowTmEnd"),
            ("estDelTmWindowTmStart", "estDelTmWindowTmEnd"),
            ("startTime", "endTime"),
        ):
            start = time_from_any(window.get(start_key))
            end = time_from_any(window.get(end_key))
            if start and end and start != end:
                return (start, end)
    return ("", "")


def fedex_events(package: dict[str, Any]) -> list[dict[str, str]]:
    source = package.get("scanEventList") or package.get("scanEvents") or []
    if not isinstance(source, list):
        return []
    events: list[dict[str, str]] = []
    for item in source[-20:]:
        if not isinstance(item, dict):
            continue
        event = {
            "timestamp": first_text(item.get("date"), item.get("dateTime")),
            "status": first_text(item.get("status"), item.get("eventDescription")),
            "location": first_text(item.get("scanLocation"), item.get("scanLocationAddress")),
        }
        events.append({key: value for key, value in event.items() if value})
    return events


def map_status(value: str) -> str:
    text = str(value or "").lower()
    if " dl " in f" {text} " or "delivered" in text or "afgeleverd" in text or "bezorgd" in text:
        return "delivered"
    if "out for delivery" in text or "on fedex vehicle for delivery" in text:
        return "expected_today"
    if "exception" in text or "clearance delay" in text or "failed" in text:
        return "unknown"
    if any(term in text for term in ("in transit", "on the way", "departed", "arrived", "local fedex facility")):
        return "in_transit"
    return "unknown"


def date_from_any(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return ""


def time_from_any(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        pass
    match = re.search(r"\b([0-2]?\d)[:.h]([0-5]\d)\b", text)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return ""


def html_to_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def error_update(
    tracking_code: str,
    tracking_url: str | None,
    error: str,
    *,
    carrier: str = "fedex",
) -> dict[str, Any]:
    update = {
        "carrier": carrier,
        "tracking_code": tracking_code,
        "status": "unknown",
        "tracking_refresh_source": "local_tracking_scraper",
        "tracking_refresh_supported": True,
        "tracking_refresh_error": error,
    }
    if tracking_url:
        update["tracking_url"] = tracking_url
    return update


def vinted_configured(settings: Settings) -> bool:
    return bool(settings.vinted_accounts)


def vinted_status_payload(app: web.Application) -> dict[str, Any]:
    settings = app["settings"]
    state = dict(app.get("vinted_login_state") or {})
    account_states = dict(app.get("vinted_login_states") or {})
    accounts = [
        {
            "key": account.key,
            "configured": True,
            "profile_exists": account.profile_dir.exists(),
            "state": account_states.get(account.key, {"status": "pending", "updated_at": None}),
        }
        for account in settings.vinted_accounts
    ]
    return {
        "auto_login": settings.vinted_auto_login,
        "configured": vinted_configured(settings),
        "account_count": len(settings.vinted_accounts),
        "profile_exists": any(account["profile_exists"] for account in accounts),
        "interval_hours": settings.vinted_login_interval_hours,
        "state": state,
        "accounts": accounts,
    }


def vinted_login_blocker(text: str) -> str:
    lowered = fold_text(text)
    if any(term in lowered for term in ("captcha", "are you human", "unusual activity", "verdachte activiteit")):
        return "captcha_required"
    if any(term in lowered for term in ("verification code", "verificatiecode", "two-factor", "2fa", "security code")):
        return "two_factor_required"
    if any(term in lowered for term in ("incorrect", "wrong password", "ongeldig", "onjuist", "verkeerd wachtwoord")):
        return "invalid_credentials"
    return ""


async def refresh_vinted_login(
    app: web.Application,
    *,
    reason: str,
    account_key: str | None = None,
) -> dict[str, Any]:
    settings: Settings = app["settings"]
    if not settings.vinted_auto_login:
        return set_vinted_login_summary(app, status="disabled", reason=reason, accounts={})
    if not vinted_configured(settings):
        return set_vinted_login_summary(app, status="missing_credentials", reason=reason, accounts={})

    accounts = list(settings.vinted_accounts)
    if account_key:
        accounts = [account for account in accounts if account.key == account_key]
        if not accounts:
            return set_vinted_login_summary(app, status="unknown_account", reason=reason, accounts={})

    lock: asyncio.Lock = app["vinted_login_lock"]
    async with lock:
        results = {}
        for account in accounts:
            results[account.key] = await run_vinted_login(app, reason=reason, account=account)
        return set_vinted_login_summary(
            app,
            status=aggregate_vinted_status(results),
            reason=reason,
            accounts=results,
        )


async def run_vinted_login(
    app: web.Application,
    *,
    reason: str,
    account: VintedAccount,
) -> dict[str, Any]:
    settings: Settings = app["settings"]
    playwright = app["playwright"]
    account.profile_dir.mkdir(parents=True, exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(account.profile_dir),
        headless=settings.headless,
        locale="nl-NL",
        args=["--disable-dev-shm-usage"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.goto(VINTED_HOME, wait_until="domcontentloaded", timeout=settings.timeout * 1000)
        await dismiss_vinted_overlays(page)
        if await vinted_page_looks_logged_in(page):
            return set_vinted_login_state(
                app,
                account=account,
                status="ok",
                reason=reason,
                detail="already_logged_in",
            )

        await page.goto(VINTED_LOGIN_URL, wait_until="domcontentloaded", timeout=settings.timeout * 1000)
        await dismiss_vinted_overlays(page)
        filled = await fill_vinted_credentials(page, account)
        if not filled:
            text = await safe_body_text(page)
            blocker = vinted_login_blocker(text)
            return set_vinted_login_state(
                app,
                account=account,
                status=blocker or "login_form_not_found",
                reason=reason,
            )

        try:
            await page.wait_for_load_state("networkidle", timeout=min(settings.timeout * 1000, 15000))
        except PlaywrightError:
            pass
        await page.wait_for_timeout(1500)
        await dismiss_vinted_overlays(page)
        text = await safe_body_text(page)
        blocker = vinted_login_blocker(text)
        if blocker:
            return set_vinted_login_state(app, account=account, status=blocker, reason=reason)
        if await vinted_page_looks_logged_in(page):
            return set_vinted_login_state(
                app,
                account=account,
                status="ok",
                reason=reason,
                detail="submitted_credentials",
            )
        return set_vinted_login_state(app, account=account, status="login_required", reason=reason)
    except PlaywrightError as err:
        LOG.warning("Vinted login refresh failed: %s", err)
        return set_vinted_login_state(
            app,
            account=account,
            status="playwright_error",
            reason=reason,
            detail=str(err)[:160],
        )
    finally:
        await context.close()


async def dismiss_vinted_overlays(page) -> None:
    for label in (
        "Alles accepteren",
        "Accepteren",
        "Accept all",
        "Accept",
        "Akkoord",
        "OK",
    ):
        try:
            await page.get_by_role("button", name=re.compile(label, re.IGNORECASE)).click(timeout=750)
            return
        except PlaywrightError:
            continue


async def fill_vinted_credentials(page, account: VintedAccount) -> bool:
    email_filled = await fill_first_locator(
        page,
        (
            'input[type="email"]',
            'input[name="email"]',
            'input[name="login"]',
            'input[name="username"]',
            'input[autocomplete="username"]',
        ),
        account.email,
    )
    password_filled = await fill_first_locator(
        page,
        (
            'input[type="password"]',
            'input[name="password"]',
            'input[autocomplete="current-password"]',
        ),
        account.password,
    )
    if not (email_filled and password_filled):
        return False

    for label in ("Inloggen", "Log in", "Aanmelden", "Sign in", "Continue", "Doorgaan"):
        try:
            await page.get_by_role("button", name=re.compile(label, re.IGNORECASE)).click(timeout=1500)
            return True
        except PlaywrightError:
            continue
    try:
        await page.locator('button[type="submit"], input[type="submit"]').first.click(timeout=1500)
        return True
    except PlaywrightError:
        return False


async def fill_first_locator(page, selectors: tuple[str, ...], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.fill(value, timeout=1500)
            return True
        except PlaywrightError:
            continue
    return False


async def vinted_page_looks_logged_in(page) -> bool:
    url = page.url.lower()
    if "login" in url or "sign-in" in url:
        return False
    for selector in (
        '[data-testid*="inbox"]',
        '[data-testid*="profile"]',
        'a[href*="/inbox"]',
        'a[href*="/member/"]',
    ):
        try:
            if await page.locator(selector).count() > 0:
                return True
        except PlaywrightError:
            continue
    text = await safe_body_text(page)
    folded = fold_text(text)
    return any(term in folded for term in ("mijn profiel", "berichten", "favorieten", "my profile", "inbox"))


async def safe_body_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=3000)
    except PlaywrightError:
        return ""


def set_vinted_login_state(
    app: web.Application,
    *,
    account: VintedAccount,
    status: str,
    reason: str,
    detail: str | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if detail:
        state["detail"] = detail
    states = dict(app.get("vinted_login_states") or {})
    states[account.key] = state
    app["vinted_login_states"] = states
    LOG.info("Vinted login refresh -> account=%s status=%s reason=%s", account.key, status, reason)
    return state


def set_vinted_login_summary(
    app: web.Application,
    *,
    status: str,
    reason: str,
    accounts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if accounts:
        state["accounts"] = accounts
    app["vinted_login_state"] = state
    LOG.info("Vinted login summary -> status=%s reason=%s accounts=%s", status, reason, len(accounts))
    return state


def aggregate_vinted_status(accounts: dict[str, dict[str, Any]]) -> str:
    statuses = [str(state.get("status") or "") for state in accounts.values()]
    if not statuses:
        return "missing_credentials"
    if all(status == "ok" for status in statuses):
        return "ok"
    if any(status == "ok" for status in statuses):
        return "partial_success"
    unique_statuses = {status for status in statuses if status}
    if len(unique_statuses) == 1:
        return unique_statuses.pop()
    return "attention_required"


async def start_vinted_login_task(app: web.Application) -> None:
    app["vinted_login_lock"] = asyncio.Lock()
    app["vinted_login_states"] = {}
    settings: Settings = app["settings"]
    if not settings.vinted_auto_login:
        app["vinted_login_state"] = {"status": "disabled", "updated_at": None}
        return
    if not vinted_configured(settings):
        app["vinted_login_state"] = {
            "status": "missing_credentials",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return
    app["vinted_login_states"] = {
        account.key: {"status": "pending", "updated_at": None} for account in settings.vinted_accounts
    }
    app["vinted_login_task"] = asyncio.create_task(vinted_login_loop(app))


async def stop_vinted_login_task(app: web.Application) -> None:
    task = app.get("vinted_login_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def vinted_login_loop(app: web.Application) -> None:
    settings: Settings = app["settings"]
    if settings.vinted_login_on_start:
        await refresh_vinted_login(app, reason="startup")
    interval = max(1, settings.vinted_login_interval_hours) * 3600
    while True:
        await asyncio.sleep(interval)
        await refresh_vinted_login(app, reason="scheduled")


async def start_browser(app: web.Application) -> None:
    playwright = await async_playwright().start()
    app["playwright"] = playwright
    app["browser"] = None
    app["browser_lock"] = asyncio.Lock()


async def get_shared_browser(app: web.Application) -> Browser:
    settings = app["settings"]
    browser = app.get("browser")
    if browser and browser.is_connected():
        return browser

    lock: asyncio.Lock = app["browser_lock"]
    async with lock:
        browser = app.get("browser")
        if browser and browser.is_connected():
            return browser
        playwright = app["playwright"]
        app["browser"] = await asyncio.wait_for(
            playwright.chromium.launch(
                headless=settings.headless,
                args=["--disable-dev-shm-usage"],
            ),
            timeout=min(max(settings.timeout, 10), 30),
        )
        return app["browser"]


async def stop_browser(app: web.Application) -> None:
    browser = app.get("browser")
    if browser:
        await browser.close()
    playwright = app.get("playwright")
    if playwright:
        await playwright.stop()


def create_app(settings: Settings | None = None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["settings"] = settings or settings_from_env()
    app["started_at"] = datetime.now(timezone.utc).isoformat()
    app.router.add_get("/health", health)
    app.router.add_get("/login/vinted/status", vinted_login_status)
    app.router.add_post("/login/vinted", vinted_login_refresh)
    app.router.add_post("/track", track)
    app.on_startup.append(start_browser)
    app.on_startup.append(start_vinted_login_task)
    app.on_cleanup.append(stop_vinted_login_task)
    app.on_cleanup.append(stop_browser)
    return app


def bind_hosts(host: str) -> str | list[str]:
    """Listen on both address families when Supervisor DNS advertises both."""

    if host in {"", "0.0.0.0", "::"}:
        return ["0.0.0.0", "::"]
    return host


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    settings = settings_from_env()
    hosts = bind_hosts(settings.host)
    LOG.info("Starting Parcels tracking scraper on %s:%s", hosts, settings.port)
    web.run_app(create_app(settings), host=hosts, port=settings.port)


if __name__ == "__main__":
    main()
