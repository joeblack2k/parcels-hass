"""Local tracking scraper sidecar for personal parcels-hass setups."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import random
import re
import time
import unicodedata
from typing import Any
from urllib.parse import quote_plus, urljoin

from aiohttp import web
from playwright.async_api import Browser, Error as PlaywrightError, async_playwright

LOG = logging.getLogger("parcels_fedex_scraper")

ADDON_OPTIONS_PATH = Path("/data/options.json")
VINTED_SESSION_STORE_PATH = Path("/data/vinted_sessions.json")
SERVICE_NAME = "parcels-tracking-scraper"
VINTED_PROFILE_DIR = Path("/data/browser-profiles/vinted")
FEDEX_HOME = "https://www.fedex.com/en-us/home.html"
FEDEX_TRACKING_PAGE = "https://www.fedex.com/fedextrack/?trknbr={tracking_code}"
CHRONOPOST_TRACKING_PAGE = "https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT={tracking_code}"
VINTED_HOME = "https://www.vinted.nl/"
VINTED_LOGIN_URL = VINTED_HOME
VINTED_PARCEL_PATHS = (
    "/inbox",
    "/my_purchases",
    "/my_orders",
)
VINTED_API_PATHS = (
    "/api/v2/conversations",
    "/api/v2/inbox/conversations",
    "/api/v2/transactions",
    "/api/v2/orders",
    "/api/v2/my_orders",
    "/api/v2/shipments",
)
VINTED_LINK_HINTS = (
    "/inbox/",
    "transaction",
    "order",
    "shipment",
    "tracking",
    "purchase",
)
VINTED_ALLOWED_COOKIE_NAMES = {
    "__cf_bm",
    "access_token_web",
    "anon_id",
    "datadome",
    "refresh_token_web",
    "v_udt",
}
VINTED_REQUIRED_COOKIE_NAMES = {"access_token_web", "refresh_token_web"}
VINTED_MAX_COOKIE_LENGTH = 20000
VINTED_CSRF_RE = re.compile(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)')
VINTED_STATUS_KEYWORDS = {
    "ready_for_pickup": (
        "ready for pickup",
        "ready to pick up",
        "available for pickup",
        "ligt klaar",
        "klaar om op te halen",
        "op te halen",
        "afhaalcode",
        "ophaalcode",
        "pickup code",
    ),
    "picked_up": (
        "picked up",
        "collected",
        "opgehaald",
        "afgehaald",
    ),
    "delivered": (
        "delivered",
        "afgeleverd",
        "bezorgd",
        "arrived",
        "aangekomen",
    ),
    "expected_today": (
        "out for delivery",
        "wordt vandaag bezorgd",
        "bezorging vandaag",
    ),
    "in_transit": (
        "in transit",
        "shipped",
        "sent",
        "on its way",
        "onderweg",
        "verzonden",
        "bezorger",
        "transport",
        "delivery",
    ),
    "cancelled": (
        "cancelled",
        "canceled",
        "geannuleerd",
        "expired",
        "verlopen",
        "retour",
        "returned",
        "teruggestuurd",
    ),
}
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
    session_cookie: str
    profile_dir: Path


class VintedApiError(Exception):
    """Base exception for Vinted API reads."""


class VintedApiAuthError(VintedApiError):
    """Raised when Vinted rejects the configured account."""


class VintedApiRateLimitError(VintedApiError):
    """Raised when Vinted rate limits the sidecar."""


class VintedApiRequestError(VintedApiError):
    """Raised when Vinted returns an unexpected response."""


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
    raw_accounts: list[tuple[str, str, str, str]] = []
    for key, email_option, password_option, cookie_option, email_env, password_env, cookie_env in (
        (
            "account_1",
            "vinted_email",
            "vinted_password",
            "vinted_session_cookie",
            "VINTED_EMAIL",
            "VINTED_PASSWORD",
            "VINTED_SESSION_COOKIE",
        ),
        (
            "account_2",
            "vinted_email_2",
            "vinted_password_2",
            "vinted_session_cookie_2",
            "VINTED_EMAIL_2",
            "VINTED_PASSWORD_2",
            "VINTED_SESSION_COOKIE_2",
        ),
    ):
        email = str(addon_options.get(email_option) or os.environ.get(email_env) or "").strip()
        password = str(addon_options.get(password_option) or os.environ.get(password_env) or "")
        session_cookie = str(addon_options.get(cookie_option) or os.environ.get(cookie_env) or "").strip()
        if (email and password) or session_cookie:
            raw_accounts.append((key, email, password, session_cookie))

    accounts: list[VintedAccount] = []
    use_legacy_single_profile = len(raw_accounts) == 1 and raw_accounts[0][0] == "account_1"
    for key, email, password, session_cookie in raw_accounts:
        profile_dir = VINTED_PROFILE_DIR if use_legacy_single_profile else VINTED_PROFILE_DIR / key
        accounts.append(
            VintedAccount(
                key=key,
                email=email,
                password=password,
                session_cookie=session_cookie,
                profile_dir=profile_dir,
            )
        )
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


async def vinted_session_update(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"success": False, "error": "invalid_payload"}, status=400)

    account_key = str(payload.get("account") or payload.get("account_key") or "account_1").strip() or "account_1"
    settings: Settings = request.app["settings"]
    if account_key not in {account.key for account in settings.vinted_accounts}:
        return web.json_response({"success": False, "error": "unknown_account"}, status=404)

    cookie = ""
    if isinstance(payload.get("cookie"), str):
        cookie = clean_vinted_cookie_string(payload["cookie"])
    if not cookie and isinstance(payload.get("cookies"), list):
        cookie = vinted_cookie_from_list(payload["cookies"])
    if not cookie:
        return web.json_response({"success": False, "error": "empty_cookie"}, status=400)
    if len(cookie) > VINTED_MAX_COOKIE_LENGTH:
        return web.json_response({"success": False, "error": "cookie_too_large"}, status=413)

    names = vinted_cookie_names(cookie)
    if not (
        names & VINTED_REQUIRED_COOKIE_NAMES
        or any(name.startswith("_vinted") and name.endswith("_session") for name in names)
    ):
        return web.json_response({"success": False, "error": "missing_login_cookie"}, status=400)

    store = load_vinted_session_store()
    store[account_key] = {
        "cookie": cookie,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_vinted_session_store(store)
    states = dict(request.app.get("vinted_login_states") or {})
    states[account_key] = {
        "status": "ok",
        "reason": "session_cookie_bridge",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "detail": "session_cookie_stored",
    }
    request.app["vinted_login_states"] = states
    return web.json_response(
        {
            "success": True,
            "account": account_key,
            "cookie_names": sorted(names),
            "stored": True,
        }
    )


async def vinted_parcels(request: web.Request) -> web.Response:
    account_key = None
    debug = request.query.get("debug") == "1"
    try:
        payload = await request.json() if request.can_read_body else {}
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        account_key = str(payload.get("account") or payload.get("account_key") or "").strip() or None
        debug = debug or parse_bool(payload.get("debug"), False)
    result = await scrape_vinted_parcels(request.app, account_key=account_key, debug=debug)
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
    match = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b", text)
    if match:
        return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
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


async def scrape_vinted_parcels(
    app: web.Application,
    *,
    account_key: str | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    settings: Settings = app["settings"]
    if not vinted_configured(settings):
        return {
            "status": "missing_credentials",
            "source": "vinted_sidecar",
            "records": [],
            "accounts": [],
        }

    accounts = list(settings.vinted_accounts)
    if account_key:
        accounts = [account for account in accounts if account.key == account_key]
        if not accounts:
            return {
                "status": "unknown_account",
                "source": "vinted_sidecar",
                "records": [],
                "accounts": [],
            }

    records: list[dict[str, Any]] = []
    account_results: list[dict[str, Any]] = []
    per_account_timeout = max(10, int(settings.timeout / max(len(accounts), 1)))
    api_timeout = min(18, max(8, per_account_timeout))
    for account in accounts:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(fetch_vinted_api_records_sync, account),
                timeout=api_timeout,
            )
        except TimeoutError:
            result = {
                "status": "api_timeout",
                "records": [],
                "diagnostics": {"method": "password_oauth", "error": "timeout"},
            }

        if result.get("status") != "ok":
            result = await run_vinted_browser_fallback(
                app,
                account=account,
                debug=debug,
                timeout=per_account_timeout,
                api_result=result,
            )

        account_records = result.get("records") if isinstance(result.get("records"), list) else []
        records.extend(record for record in account_records if isinstance(record, dict))
        account_results.append(
            {
                "key": account.key,
                "status": result.get("status") or "unknown",
                "record_count": len(account_records),
                "diagnostics": result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    deduped = dedupe_vinted_records(records)
    status = aggregate_vinted_scrape_status(account_results)
    LOG.info("Vinted parcel scrape -> status=%s records=%s accounts=%s", status, len(deduped), len(account_results))
    return {
        "status": status,
        "source": "vinted_sidecar",
        "records": deduped,
        "accounts": account_results,
        "record_count": len(deduped),
    }


def vinted_account_state_ok(app: web.Application, account: VintedAccount) -> bool:
    return vinted_account_state(app, account).get("status") == "ok"


def vinted_account_state(app: web.Application, account: VintedAccount) -> dict[str, Any]:
    states = app.get("vinted_login_states") if isinstance(app.get("vinted_login_states"), dict) else {}
    state = states.get(account.key) if isinstance(states, dict) else None
    return state if isinstance(state, dict) else {}


def vinted_login_needs_manual_attention(status: str) -> bool:
    return status in {
        "captcha_required",
        "two_factor_required",
        "invalid_credentials",
    }


def allowed_vinted_cookie_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("_vinted") or lowered in VINTED_ALLOWED_COOKIE_NAMES


def clean_vinted_cookie_string(raw_cookie: str) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for part in str(raw_cookie or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value or name in seen or not allowed_vinted_cookie_name(name):
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def vinted_cookie_from_list(cookies: list[Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        if not name or not value or name in seen or not allowed_vinted_cookie_name(name):
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def vinted_cookie_names(cookie: str) -> set[str]:
    return {part.split("=", 1)[0].strip() for part in str(cookie or "").split(";") if "=" in part}


def load_vinted_session_store(path: Path = VINTED_SESSION_STORE_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as err:
        LOG.warning("Could not read Vinted session store: %s", err)
        return {}
    return payload if isinstance(payload, dict) else {}


def write_vinted_session_store(store: dict[str, dict[str, str]], path: Path = VINTED_SESSION_STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, ensure_ascii=True, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def stored_vinted_session_cookie(account_key: str) -> str:
    entry = load_vinted_session_store().get(account_key)
    if not isinstance(entry, dict):
        return ""
    return clean_vinted_cookie_string(str(entry.get("cookie") or ""))


async def run_vinted_browser_fallback(
    app: web.Application,
    *,
    account: VintedAccount,
    debug: bool,
    timeout: int,
    api_result: dict[str, Any],
) -> dict[str, Any]:
    settings: Settings = app["settings"]
    state = vinted_account_state(app, account)
    state_status = str(state.get("status") or "")
    if state_status != "ok" and not debug:
        diagnostics = {
            "browser_skipped": True,
            "login_state": state_status or "pending",
            "api_client": api_result.get("diagnostics") if isinstance(api_result.get("diagnostics"), dict) else {},
        }
        return {
            "status": api_result.get("status") or state_status or "login_required",
            "records": [],
            "diagnostics": diagnostics,
        }

    lock: asyncio.Lock = app["vinted_login_lock"]
    try:
        await asyncio.wait_for(lock.acquire(), timeout=min(3, max(1, int(timeout / 6))))
    except TimeoutError:
        return {
            "status": api_result.get("status") or "browser_busy",
            "records": [],
            "diagnostics": {
                "browser_locked": True,
                "api_client": api_result.get("diagnostics") if isinstance(api_result.get("diagnostics"), dict) else {},
            },
        }
    try:
        if vinted_login_needs_manual_attention(state_status):
            return {
                "status": state_status,
                "records": [],
                "diagnostics": {
                    "login_state": state_status,
                    "api_client": api_result.get("diagnostics") if isinstance(api_result.get("diagnostics"), dict) else {},
                },
            }
        if settings.vinted_auto_login and state_status != "ok":
            await run_vinted_login(app, reason="parcel_scrape_preflight", account=account)
        try:
            result = await asyncio.wait_for(
                run_vinted_parcel_scrape(app, account=account, debug=debug, use_api=False, api_result=api_result),
                timeout=timeout,
            )
        except TimeoutError:
            result = {
                "status": "timeout",
                "records": [],
                "diagnostics": {
                    "api_client": api_result.get("diagnostics") if isinstance(api_result.get("diagnostics"), dict) else {},
                },
            }
        if result.get("status") == "login_required" and settings.vinted_auto_login:
            await run_vinted_login(app, reason="parcel_scrape_retry", account=account)
            try:
                result = await asyncio.wait_for(
                    run_vinted_parcel_scrape(app, account=account, debug=debug, use_api=False, api_result=api_result),
                    timeout=timeout,
                )
            except TimeoutError:
                result = {
                    "status": "timeout",
                    "records": [],
                    "diagnostics": {
                        "api_client": api_result.get("diagnostics")
                        if isinstance(api_result.get("diagnostics"), dict)
                        else {},
                    },
                }
        return result
    finally:
        lock.release()


def fetch_vinted_api_records_sync(account: VintedAccount) -> dict[str, Any]:
    request_timeout = 8
    session_cookie = account.session_cookie or stored_vinted_session_cookie(account.key)
    diagnostics: dict[str, Any] = {
        "method": "session_cookie" if session_cookie else "password_oauth",
        "inbox_summaries": 0,
        "conversations_checked": 0,
        "packages": 0,
    }
    try:
        import cloudscraper
        from requests.cookies import RequestsCookieJar
    except ImportError:
        return {
            "status": "dependency_missing",
            "records": [],
            "diagnostics": {**diagnostics, "error": "cloudscraper_or_requests_missing"},
        }

    session = cloudscraper.create_scraper()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
    )
    request_count = 0
    cookie_values: dict[str, str] = {}

    def request(method: str, url: str, **kwargs: Any):
        nonlocal request_count
        if request_count:
            time.sleep(random.uniform(0.05, 0.2))
        request_count += 1
        return session.request(method, url, **kwargs)

    def response_json(response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as err:
            raise VintedApiRequestError("vinted_non_json_response") from err
        return data if isinstance(data, dict) else {}

    def get_json(endpoint: str) -> dict[str, Any]:
        response = request("get", urljoin(VINTED_HOME, endpoint), timeout=request_timeout)
        if response.status_code in (401, 403):
            raise VintedApiAuthError("vinted_api_auth_rejected")
        if response.status_code == 429:
            raise VintedApiRateLimitError("vinted_api_rate_limited")
        if response.status_code >= 400:
            raise VintedApiRequestError(f"vinted_api_http_{response.status_code}")
        return response_json(response)

    def apply_session_cookie(cookie: str) -> None:
        jar = RequestsCookieJar()
        if "=" not in cookie:
            cookie_values["_vinted_fr_session"] = cookie
            jar.set("_vinted_fr_session", cookie, domain="www.vinted.nl", path="/")
        else:
            for part in cookie.split(";"):
                if "=" not in part:
                    continue
                name, value = part.strip().split("=", 1)
                name = name.strip()
                value = value.strip()
                if not name or not value or not allowed_vinted_cookie_name(name):
                    continue
                cookie_values[name] = value
                jar.set(name, value, domain="www.vinted.nl", path="/")
        session.cookies.update(jar)
        access_token = cookie_values.get("access_token_web")
        if access_token:
            session.headers["Authorization"] = f"Bearer {access_token}"

    def refresh_access_token() -> bool:
        refresh_token = cookie_values.get("refresh_token_web")
        if not refresh_token:
            return False
        response = request(
            "post",
            urljoin(VINTED_HOME, "/oauth/token"),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": "web",
            },
            timeout=request_timeout,
        )
        if response.status_code in (400, 401, 403):
            return False
        if response.status_code == 429:
            raise VintedApiRateLimitError("vinted_refresh_rate_limited")
        if response.status_code >= 400:
            raise VintedApiRequestError(f"vinted_refresh_http_{response.status_code}")
        data = response_json(response)
        access_token = str(data.get("access_token") or "").strip()
        new_refresh_token = str(data.get("refresh_token") or "").strip()
        if not access_token:
            return False
        cookie_values["access_token_web"] = access_token
        session.headers["Authorization"] = f"Bearer {access_token}"
        session.cookies.set("access_token_web", access_token, domain="www.vinted.nl", path="/")
        if new_refresh_token:
            cookie_values["refresh_token_web"] = new_refresh_token
            session.cookies.set("refresh_token_web", new_refresh_token, domain="www.vinted.nl", path="/")
        diagnostics["method"] = "session_cookie_refresh"
        return True

    def login_with_password() -> None:
        if not (account.email and account.password):
            raise VintedApiAuthError("vinted_missing_password_credentials")
        login = request(
            "post",
            urljoin(VINTED_HOME, "/oauth/token"),
            data={
                "grant_type": "password",
                "username": account.email,
                "password": account.password,
                "scope": "public",
                "client_id": "web",
            },
            timeout=request_timeout,
        )
        if login.status_code in (401, 403):
            raise VintedApiAuthError("vinted_password_auth_rejected")
        if login.status_code == 429:
            raise VintedApiRateLimitError("vinted_login_rate_limited")
        if login.status_code >= 400:
            raise VintedApiRequestError(f"vinted_login_http_{login.status_code}")

        login_data = response_json(login)
        token = str(login_data.get("access_token") or "").strip()
        if token:
            session.headers["Authorization"] = f"Bearer {token}"

    try:
        home = request("get", VINTED_HOME, timeout=request_timeout)
        if home.status_code == 429:
            raise VintedApiRateLimitError("vinted_login_rate_limited")
        if home.status_code >= 500:
            raise VintedApiRequestError(f"vinted_home_http_{home.status_code}")
        csrf = VINTED_CSRF_RE.search(home.text or "")
        if csrf:
            session.headers["X-CSRF-Token"] = csrf.group(1)

        if session_cookie:
            apply_session_cookie(session_cookie)
            refresh_access_token()
        else:
            login_with_password()

        inbox = get_json("/api/v2/inbox")
        summaries = inbox.get("conversations")
        summaries = [item for item in summaries[:25] if isinstance(item, dict)] if isinstance(summaries, list) else []
        diagnostics["inbox_summaries"] = len(summaries)

        records: list[dict[str, Any]] = []
        for summary in summaries:
            conversation_id = summary.get("id")
            if conversation_id in (None, ""):
                continue
            try:
                detail = get_json(f"/api/v2/conversations/{conversation_id}")
            except VintedApiError as err:
                LOG.debug("Skipping Vinted conversation %s for %s: %s", conversation_id, account.key, err)
                continue
            diagnostics["conversations_checked"] += 1
            package = vinted_package_from_conversation(summary, detail)
            if not package:
                continue
            record = vinted_record_from_api_package(
                package,
                account_key=account.key,
                source_url=urljoin(VINTED_HOME, f"/inbox/{conversation_id}"),
            )
            if record:
                records.append(record)

        deduped = dedupe_vinted_records(records)
        diagnostics["packages"] = len(deduped)
        return {
            "status": "ok",
            "records": deduped,
            "diagnostics": diagnostics,
        }
    except VintedApiAuthError as err:
        return {"status": "api_auth_failed", "records": [], "diagnostics": {**diagnostics, "error": str(err)}}
    except VintedApiRateLimitError as err:
        return {"status": "rate_limited", "records": [], "diagnostics": {**diagnostics, "error": str(err)}}
    except VintedApiRequestError as err:
        return {"status": "api_error", "records": [], "diagnostics": {**diagnostics, "error": str(err)}}
    except Exception as err:
        LOG.warning("Vinted API scrape failed for %s: %s", account.key, err)
        return {"status": "api_error", "records": [], "diagnostics": {**diagnostics, "error": type(err).__name__}}


async def run_vinted_parcel_scrape(
    app: web.Application,
    *,
    account: VintedAccount,
    debug: bool = False,
    use_api: bool = True,
    api_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings: Settings = app["settings"]
    if use_api:
        api_result = await asyncio.to_thread(fetch_vinted_api_records_sync, account)
        if api_result.get("status") == "ok":
            return api_result
    api_result = api_result or {}
    api_diagnostics = {
        "status": api_result.get("status") or "unknown",
        **(api_result.get("diagnostics") if isinstance(api_result.get("diagnostics"), dict) else {}),
    }

    playwright = app["playwright"]
    account.profile_dir.mkdir(parents=True, exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(account.profile_dir),
        headless=settings.headless,
        locale="nl-NL",
        args=["--disable-dev-shm-usage"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    json_payloads: list[tuple[str, Any]] = []
    page_texts: list[tuple[str, str]] = []
    detail_links: list[str] = []
    diagnostics: dict[str, Any] = {
        "api_client": api_diagnostics,
        "api_payloads": 0,
        "pages": 0,
        "detail_links": 0,
        "text_chars": 0,
        "json_payloads": 0,
    }
    debug_samples: list[str] = []

    async def capture_response(response) -> None:
        if not is_vinted_json_response(response.url):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        json_payloads.append((response.url, payload))

    page.on("response", lambda response: asyncio.create_task(capture_response(response)))

    try:
        await page.goto(VINTED_HOME, wait_until="domcontentloaded", timeout=vinted_page_timeout(settings) * 1000)
        await dismiss_vinted_overlays(page)
        if not await vinted_page_looks_logged_in(page):
            return {"status": "login_required", "records": [], "diagnostics": diagnostics}

        for source_url, payload in await fetch_vinted_api_payloads(context, settings):
            json_payloads.append((source_url, payload))
        diagnostics["api_payloads"] = len(json_payloads)

        for path in VINTED_PARCEL_PATHS:
            url = urljoin(VINTED_HOME, path)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=vinted_page_timeout(settings) * 1000)
            except PlaywrightError:
                continue
            await dismiss_vinted_overlays(page)
            await wait_for_vinted_settle(page, settings.timeout)
            text = await safe_body_text(page)
            if text:
                page_texts.append((url, text))
                diagnostics["text_chars"] += len(text)
                if debug:
                    debug_samples.append(redact_vinted_debug_text(f"{url}\n{text}")[:1200])
            for link in await extract_vinted_detail_links(page):
                if link not in detail_links:
                    detail_links.append(link)
            diagnostics["pages"] += 1

        diagnostics["detail_links"] = len(detail_links)
        for url in detail_links[:2]:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=vinted_page_timeout(settings) * 1000)
            except PlaywrightError:
                continue
            await dismiss_vinted_overlays(page)
            await wait_for_vinted_settle(page, settings.timeout)
            text = await safe_body_text(page)
            if text:
                page_texts.append((url, text))
                diagnostics["text_chars"] += len(text)
                if debug:
                    debug_samples.append(redact_vinted_debug_text(f"{url}\n{text}")[:1200])
            diagnostics["pages"] += 1

        diagnostics["json_payloads"] = len(json_payloads)
        if debug:
            diagnostics["samples"] = debug_samples[:4]
        records: list[dict[str, Any]] = []
        for source_url, payload in json_payloads:
            records.extend(vinted_records_from_json(payload, account_key=account.key, source_url=source_url))
        if records:
            return {"status": "ok", "records": dedupe_vinted_records(records), "diagnostics": diagnostics}
        for source_url, text in page_texts:
            record = vinted_record_from_text(text, account_key=account.key, source_url=source_url)
            if record:
                records.append(record)
        return {"status": "ok", "records": dedupe_vinted_records(records), "diagnostics": diagnostics}
    except PlaywrightError as err:
        LOG.warning("Vinted parcel scrape failed for %s: %s", account.key, err)
        return {"status": "playwright_error", "records": [], "detail": str(err)[:160], "diagnostics": diagnostics}
    finally:
        await context.close()


async def wait_for_vinted_settle(page, timeout: int) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout * 1000, 5000))
    except PlaywrightError:
        pass
    await page.wait_for_timeout(500)


def vinted_page_timeout(settings: Settings) -> int:
    return min(max(int(settings.timeout / 4), 5), 10)


async def fetch_vinted_api_payloads(context, settings: Settings) -> list[tuple[str, Any]]:
    payloads: list[tuple[str, Any]] = []
    timeout_ms = vinted_page_timeout(settings) * 1000
    for path in VINTED_API_PATHS:
        url = urljoin(VINTED_HOME, path)
        try:
            response = await context.request.get(
                url,
                headers={"Accept": "application/json"},
                timeout=timeout_ms,
            )
        except PlaywrightError:
            continue
        if response.status >= 400:
            continue
        try:
            payload = await response.json()
        except Exception:
            continue
        payloads.append((url, payload))
    return payloads


async def extract_vinted_detail_links(page) -> list[str]:
    try:
        links = await page.locator("a[href]").evaluate_all(
            """
            els => els
              .map(a => a.href || "")
              .filter(h => h.includes("vinted.nl"))
              .filter(h => /(inbox|transaction|order|shipment|tracking|purchase)/i.test(h))
            """
        )
    except PlaywrightError:
        return []
    results: list[str] = []
    for link in links:
        text = str(link or "").split("#", 1)[0]
        if not text or text in results:
            continue
        if any(hint in text.lower() for hint in VINTED_LINK_HINTS):
            results.append(text)
    return results[:30]


def is_vinted_json_response(url: str) -> bool:
    lowered = str(url or "").lower()
    return "vinted.nl" in lowered and "/api/" in lowered and any(
        hint in lowered for hint in ("transaction", "order", "shipment", "tracking", "inbox", "purchase")
    )


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
    if account.session_cookie or stored_vinted_session_cookie(account.key):
        return set_vinted_login_state(
            app,
            account=account,
            status="ok",
            reason=reason,
            detail="session_cookie_configured",
        )
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
        await reveal_vinted_email_login_form(page)
        filled = await fill_vinted_credentials(page, account)
        if not filled:
            text = await safe_body_text(page)
            blocker = vinted_login_blocker(text)
            return set_vinted_login_state(
                app,
                account=account,
                status=blocker or "login_form_not_found",
                reason=reason,
                detail=redact_vinted_debug_text(text)[:300],
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


async def reveal_vinted_email_login_form(page) -> None:
    for label in (
        "Inloggen",
        "Log in",
        "Aanmelden",
        "Sign in",
        "Heb je al een account? Inloggen",
    ):
        if await click_vinted_control(page, label):
            await page.wait_for_timeout(600)
            break

    for label in (
        "E-mail",
        "Email",
        "Log in with email",
        "Inloggen met e-mail",
        "Ga verder met e-mail",
        "Continue with email",
        "Of meld je aan met e-mail",
    ):
        if await click_vinted_control(page, label):
            await page.wait_for_timeout(600)
            return


async def click_vinted_control(page, label: str) -> bool:
    pattern = re.compile(re.escape(label), re.IGNORECASE)
    for role in ("button", "link"):
        try:
            await page.get_by_role(role, name=pattern).click(timeout=1200)
            return True
        except PlaywrightError:
            continue
    try:
        await page.get_by_text(pattern).click(timeout=1200)
        return True
    except PlaywrightError:
        return False


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
    text = await safe_body_text(page)
    folded = fold_text(text)
    if vinted_page_looks_logged_out(folded):
        return False
    for selector in (
        '[data-testid*="inbox"]',
        '[data-testid*="profile"]',
        'a[href*="/inbox"]',
        'a[href*="/member/"]:not([href*="signup"]):not([href*="login"]):not([href*="select_type"])',
    ):
        try:
            if await page.locator(selector).count() > 0:
                return True
        except PlaywrightError:
            continue
    return any(term in folded for term in ("mijn profiel", "berichten", "favorieten", "my profile", "inbox"))


def vinted_page_looks_logged_out(folded_text: str) -> bool:
    return any(
        term in folded_text
        for term in (
            "registreren | inloggen",
            "word lid en verkoop",
            "ga verder met google",
            "ga verder met apple",
            "heb je al een account? inloggen",
            "of meld je aan met",
        )
    )


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


def aggregate_vinted_scrape_status(accounts: list[dict[str, Any]]) -> str:
    statuses = [str(account.get("status") or "") for account in accounts]
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


def vinted_package_from_conversation(
    summary: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any] | None:
    conversation = detail.get("conversation") if isinstance(detail.get("conversation"), dict) else detail
    if not isinstance(conversation, dict):
        conversation = {}
    transaction = conversation.get("transaction")
    transaction = transaction if isinstance(transaction, dict) else {}
    shipment = transaction.get("shipment")
    shipment = shipment if isinstance(shipment, dict) else {}
    order = transaction.get("order")
    order = order if isinstance(order, dict) else {}
    combined = {
        "summary": summary,
        "conversation": conversation,
        "transaction": transaction,
        "shipment": shipment,
        "order": order,
    }
    text = "\n".join(flatten_strings(combined, limit=300))
    structured_status = vinted_first_by_keys(
        combined,
        (
            "shipment_status",
            "tracking_status",
            "delivery_status",
            "order_status",
            "transaction_status",
            "status_title",
            "status",
            "state",
        ),
    )
    status = normalize_vinted_structured_status(structured_status, text)
    tracking_code = vinted_as_str(
        vinted_first_by_keys(
            combined,
            (
                "tracking_code",
                "tracking_number",
                "tracking_id",
                "shipment_tracking_code",
                "parcel_number",
            ),
        )
    )
    carrier = vinted_as_str(
        vinted_first_by_keys(
            combined,
            (
                "carrier",
                "carrier_name",
                "shipping_provider",
                "provider_name",
                "delivery_company",
                "shipment_provider",
            ),
        )
    )
    pickup_point = vinted_as_str(
        vinted_first_by_keys(
            combined,
            (
                "pickup_point",
                "pickup_point_name",
                "collection_point",
                "parcel_shop",
                "locker_name",
            ),
        )
    )
    pickup_code = vinted_as_str(vinted_first_key_contains(combined, ("pickup", "code")))
    pickup_code = pickup_code or vinted_as_str(vinted_first_key_contains(combined, ("collection", "code")))
    pickup_code = pickup_code or vinted_as_str(vinted_first_key_contains(combined, ("locker", "code")))
    pickup_code = pickup_code or vinted_pickup_code(text) or None
    pickup_deadline = vinted_as_str(
        vinted_first_by_keys(
            combined,
            (
                "pickup_deadline",
                "pickup_until",
                "pickup_by",
                "collection_deadline",
                "expires_at",
                "expiration_date",
            ),
        )
    )
    expected_from, expected_to = vinted_expected_delivery_range(combined, text)
    tracking_events = vinted_extract_tracking_events(combined, text)
    item_title = vinted_extract_item_title(combined)
    item_title = item_title or vinted_item_title_from_text(text)
    other_party = vinted_extract_other_party(combined)
    other_party = other_party or vinted_other_party_from_text(text)
    last_update = vinted_as_str(
        vinted_first_by_keys(
            combined,
            (
                "status_updated_at",
                "updated_at",
                "last_message_at",
                "last_update",
                "created_at",
            ),
        )
    )
    conversation_id = summary.get("id") or conversation.get("id")
    package_id = str(
        shipment.get("id")
        or transaction.get("id")
        or conversation_id
        or tracking_code
        or vinted_stable_text_id(f"{item_title or ''} {other_party or ''} {text[:200]}")
    )
    if (
        status == "unknown"
        and not any((tracking_code, carrier, pickup_code, pickup_point, pickup_deadline))
        and not looks_like_vinted_parcel_text(text)
    ):
        return None
    return {
        "package_id": package_id,
        "thread_id": str(conversation_id) if conversation_id is not None else None,
        "status": status,
        "item_title": item_title,
        "other_party": other_party,
        "carrier": carrier,
        "tracking_code": tracking_code,
        "pickup_point": pickup_point,
        "pickup_deadline": date_from_any(pickup_deadline) or pickup_deadline,
        "pickup_code": re.sub(r"[^A-Z0-9]", "", pickup_code.upper()) if pickup_code else None,
        "expected_date": expected_from,
        "expected_date_to": expected_to,
        "tracking_events": tracking_events,
        "last_update": last_update,
        "source_confidence": "structured" if structured_status or transaction or shipment else "text",
        "raw_text": text[:600],
    }


def vinted_record_from_api_package(
    package: dict[str, Any],
    *,
    account_key: str,
    source_url: str,
) -> dict[str, Any] | None:
    status = str(package.get("status") or "unknown")
    pickup_code = vinted_as_str(package.get("pickup_code"))
    pickup_location = vinted_as_str(package.get("pickup_point"))
    if pickup_code and status in {"unknown", "in_transit", "expected_today"}:
        status = "ready_for_pickup"

    carrier_reference = vinted_carrier_tracking_from_values(
        carrier=vinted_as_str(package.get("carrier")),
        tracking_code=vinted_as_str(package.get("tracking_code")),
        text=str(package.get("raw_text") or ""),
    )
    package_id = vinted_as_str(package.get("package_id")) or vinted_stable_text_id(str(package))
    tracking_code = (
        carrier_reference.get("tracking_code")
        if carrier_reference
        else vinted_as_str(package.get("tracking_code")) or package_id
    )
    if status == "unknown" and not any((carrier_reference, pickup_code, pickup_location)):
        return None

    expected_date = vinted_as_str(package.get("expected_date"))
    expected_date_to = vinted_as_str(package.get("expected_date_to"))
    record: dict[str, Any] = {
        "carrier": "vinted",
        "shop": "Vinted",
        "tracking_code": tracking_code,
        "status": status,
        "source": "vinted_sidecar",
        "confidence": "high",
        "tracking_url": carrier_reference.get("tracking_url") if carrier_reference else source_url,
        "tracking_status_text": vinted_api_status_text(package, status=status),
        "tracking_refresh_source": "vinted_sidecar_api",
        "tracking_refresh_supported": True,
        "extra": {
            "vinted_account": account_key,
            "vinted_source_url": source_url,
            "vinted_id": package_id,
            "vinted_source_confidence": package.get("source_confidence") or "unknown",
        },
    }
    if expected_date and status not in {"ready_for_pickup", "picked_up", "delivered", "cancelled"}:
        record["expected_date"] = expected_date
    if expected_date_to:
        record["extra"]["expected_date_end"] = expected_date_to
        record["extra"]["vinted_expected_date_to"] = expected_date_to
    tracking_events = package.get("tracking_events")
    if isinstance(tracking_events, list) and tracking_events:
        record["extra"]["tracking_events"] = tracking_events[:10]
    for extra_key in ("thread_id", "item_title", "other_party", "last_update"):
        if package.get(extra_key):
            record["extra"][f"vinted_{extra_key}"] = package[extra_key]
    if pickup_location and status == "ready_for_pickup":
        record["pickup_location"] = clean_vinted_location(pickup_location)
    if pickup_code and status == "ready_for_pickup":
        record["pickup_code"] = pickup_code
    if package.get("pickup_deadline"):
        record["extra"]["pickup_deadline"] = package["pickup_deadline"]
    if carrier_reference:
        record["extra"]["carrier_tracking"] = carrier_reference
    return record


def normalize_vinted_structured_status(*values: Any) -> str:
    haystack = " ".join(str(value).replace("_", " ").lower() for value in values if value not in (None, "", []))
    if not haystack:
        return "unknown"
    for status, keywords in VINTED_STATUS_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            return status
    normalized = re.sub(r"\s+", "_", haystack.strip())
    if normalized in {"ready_for_pickup", "picked_up", "delivered", "expected_today", "in_transit", "cancelled"}:
        return normalized
    return "unknown"


def vinted_first_by_keys(node: Any, keys: tuple[str, ...]) -> Any:
    wanted = {key.lower() for key in keys}
    for candidate in iter_dicts(node):
        for key, value in candidate.items():
            if key.lower() in wanted and value not in (None, "", []):
                return value
    return None


def vinted_first_key_contains(node: Any, all_terms: tuple[str, ...]) -> Any:
    terms = tuple(term.lower() for term in all_terms)
    for candidate in iter_dicts(node):
        for key, value in candidate.items():
            if all(term in key.lower() for term in terms) and value not in (None, "", []):
                return value
    return None


def vinted_as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


VINTED_MONTHS = {
    "januari": 1,
    "jan": 1,
    "january": 1,
    "februari": 2,
    "feb": 2,
    "february": 2,
    "maart": 3,
    "mrt": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "mei": 5,
    "may": 5,
    "juni": 6,
    "jun": 6,
    "june": 6,
    "juli": 7,
    "jul": 7,
    "july": 7,
    "augustus": 8,
    "aug": 8,
    "august": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "oktober": 10,
    "okt": 10,
    "oct": 10,
    "october": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def vinted_expected_delivery_range(node: Any, text: str) -> tuple[str, str]:
    start = vinted_date_from_any(
        vinted_first_by_keys(
            node,
            (
                "expected_delivery_from",
                "expected_delivery_start",
                "estimated_delivery_from",
                "estimated_delivery_start",
                "delivery_from",
                "delivery_start",
                "min_delivery_date",
                "expected_from",
                "from_date",
            ),
        )
    )
    end = vinted_date_from_any(
        vinted_first_by_keys(
            node,
            (
                "expected_delivery_to",
                "expected_delivery_end",
                "estimated_delivery_to",
                "estimated_delivery_end",
                "delivery_to",
                "delivery_end",
                "max_delivery_date",
                "expected_to",
                "to_date",
            ),
        )
    )
    if start or end:
        return (start or end, end or start)

    return vinted_date_range_from_text(text)


def vinted_date_from_any(value: Any) -> str:
    iso = date_from_any(value)
    if iso:
        return iso
    text = fold_text(str(value or ""))
    year = vinted_context_year(text)
    month_names = "|".join(sorted(map(re.escape, VINTED_MONTHS), key=len, reverse=True))
    for pattern in (
        rf"\b(?P<month>{month_names})\s+(?P<day>\d{{1,2}})(?:,\s*(?P<year>20\d{{2}}))?\b",
        rf"\b(?P<day>\d{{1,2}})\s+(?P<month>{month_names})(?:\s+(?P<year>20\d{{2}}))?\b",
    ):
        match = re.search(pattern, text)
        if match:
            return vinted_iso_date(
                int(match.group("day")),
                VINTED_MONTHS[match.group("month")],
                int(match.group("year") or year),
            )
    return ""


def vinted_date_range_from_text(text: str) -> tuple[str, str]:
    folded = fold_text(text)
    match = re.search(r"(verwachte levertijd|verwachte bezorging|expected delivery|estimated delivery)[:\s]+(.{0,90})", folded)
    candidates = [match.group(2)] if match else []
    candidates.append(folded)
    year = vinted_context_year(folded)
    month_names = "|".join(sorted(map(re.escape, VINTED_MONTHS), key=len, reverse=True))

    for candidate in candidates:
        for pattern in (
            rf"\b(?P<m1>{month_names})\s+(?P<d1>\d{{1,2}})\s*(?:-|–|—|t/m|tot)\s*(?:(?P<m2>{month_names})\s+)?(?P<d2>\d{{1,2}})(?:,?\s*(?P<year>20\d{{2}}))?\b",
            rf"\b(?P<d1>\d{{1,2}})\s+(?P<m1>{month_names})\s*(?:-|–|—|t/m|tot)\s*(?P<d2>\d{{1,2}})(?:\s+(?P<m2>{month_names}))?(?:\s+(?P<year>20\d{{2}}))?\b",
        ):
            range_match = re.search(pattern, candidate)
            if not range_match:
                continue
            used_year = int(range_match.group("year") or year)
            start_month = VINTED_MONTHS[range_match.group("m1")]
            end_month = VINTED_MONTHS[range_match.group("m2") or range_match.group("m1")]
            start = vinted_iso_date(int(range_match.group("d1")), start_month, used_year)
            end = vinted_iso_date(int(range_match.group("d2")), end_month, used_year)
            if start and end:
                return (start, end)

        single = vinted_date_from_any(candidate)
        if single and match:
            return (single, single)
    return ("", "")


def vinted_context_year(text: str) -> int:
    match = re.search(r"\b(20\d{2})\b", str(text or ""))
    return int(match.group(1)) if match else datetime.now().year


def vinted_iso_date(day: int, month: int, year: int) -> str:
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def vinted_extract_tracking_events(node: Any, text: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for candidate in iter_dicts(node):
        status = vinted_as_str(
            candidate.get("status_title")
            or candidate.get("status_text")
            or candidate.get("tracking_status")
            or candidate.get("title")
            or candidate.get("label")
            or candidate.get("description")
            or candidate.get("message")
            or candidate.get("status")
        )
        if not status or not vinted_is_tracking_event_status(status):
            continue
        timestamp = vinted_datetime_from_any(
            candidate.get("created_at")
            or candidate.get("updated_at")
            or candidate.get("happened_at")
            or candidate.get("date")
            or candidate.get("timestamp")
        )
        if not timestamp:
            continue
        event = {"status": clean_location_piece(status)[:120], "timestamp": timestamp}
        location = vinted_as_str(candidate.get("location") or candidate.get("place") or candidate.get("city"))
        if location:
            event["location"] = clean_location_piece(location)[:120]
        events.append(event)

    events.extend(vinted_tracking_events_from_text(text))
    return dedupe_vinted_events(events)[:10]


def vinted_datetime_from_any(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(microsecond=0).isoformat()
    except ValueError:
        pass
    match = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2}),?\s+([0-2]?\d)[:.]([0-5]\d)\b", text)
    if match:
        return (
            f"{int(match.group(3)):04d}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
            f"T{int(match.group(4)):02d}:{match.group(5)}:00"
        )
    return ""


def vinted_tracking_events_from_text(text: str) -> list[dict[str, str]]:
    normalized = re.sub(r"\s+", " ", html.unescape(str(text or "")))
    status_pattern = (
        r"Onderweg|Verzonden|Trackingcode aangemaakt(?:\s*-\s*[A-Z0-9]{8,40})?|"
        r"In transit|Shipped|Tracking code created(?:\s*-\s*[A-Z0-9]{8,40})?|"
        r"Klaar om op te halen|Ready for pickup|Afgeleverd|Delivered"
    )
    events: list[dict[str, str]] = []
    for match in re.finditer(
        rf"\b({status_pattern})\b\s+(\d{{1,2}}[-/]\d{{1,2}}[-/]20\d{{2}}),?\s+([0-2]?\d[:.][0-5]\d)",
        normalized,
        re.IGNORECASE,
    ):
        status = clean_location_piece(match.group(1))
        timestamp = vinted_datetime_from_any(f"{match.group(2)}, {match.group(3)}")
        if not timestamp:
            continue
        event = {"status": vinted_event_status_label(status), "timestamp": timestamp}
        code_match = re.search(r"\b([A-Z0-9]{8,40})\b", status)
        if code_match:
            event["tracking_code"] = code_match.group(1).upper()
        events.append(event)
    return events


def vinted_event_status_label(value: str) -> str:
    folded = fold_text(value)
    if "trackingcode aangemaakt" in folded or "tracking code created" in folded:
        return "Trackingcode aangemaakt"
    if "onderweg" in folded or "in transit" in folded:
        return "Onderweg"
    if "verzonden" in folded or "shipped" in folded:
        return "Verzonden"
    if "klaar" in folded or "ready for pickup" in folded:
        return "Klaar om op te halen"
    if "afgeleverd" in folded or "delivered" in folded:
        return "Afgeleverd"
    return clean_location_piece(value)[:120]


def vinted_is_tracking_event_status(value: str) -> bool:
    folded = fold_text(value)
    return any(
        term in folded
        for term in (
            "onderweg",
            "verzonden",
            "trackingcode aangemaakt",
            "tracking code created",
            "in transit",
            "shipped",
            "ready for pickup",
            "klaar om op te halen",
            "afgeleverd",
            "delivered",
        )
    )


def dedupe_vinted_events(events: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        status = clean_location_piece(str(event.get("status") or ""))
        timestamp = str(event.get("timestamp") or "")
        if not status or not timestamp:
            continue
        key = (fold_text(status), timestamp)
        if key in seen:
            continue
        seen.add(key)
        clean_event = {"status": status[:120], "timestamp": timestamp}
        for extra_key in ("location", "tracking_code"):
            value = clean_location_piece(str(event.get(extra_key) or ""))
            if value:
                clean_event[extra_key] = value[:120]
        deduped.append(clean_event)
    return deduped


def vinted_extract_item_title(node: Any) -> str | None:
    if isinstance(node, dict):
        for section in ("transaction", "order", "summary", "conversation"):
            candidate = node.get(section)
            if not isinstance(candidate, dict):
                continue
            for key in ("item_title", "title", "description", "subtitle"):
                title = vinted_as_str(candidate.get(key))
                if title and len(title) <= 120:
                    return title

    for candidate in iter_dicts(node):
        item = candidate.get("item")
        if isinstance(item, dict):
            title = vinted_as_str(item.get("title"))
            if title:
                return title
        for key in ("item_title", "title", "subject"):
            title = vinted_as_str(candidate.get(key))
            if title and len(title) <= 120:
                return title
    return None


def vinted_item_title_from_text(text: str) -> str | None:
    skip = {
        "pakket volgen",
        "aankoop geslaagd",
        "bestelling verzonden",
        "trackinginformatie",
        "trackingnummer",
        "verzend een bericht",
    }
    for line in meaningful_lines(text)[:30]:
        folded = fold_text(line)
        if folded in skip or folded.startswith("eur ") or folded.startswith("€"):
            continue
        if re.fullmatch(r"\d+\s+(?:artikel|artikelen|item|items)", folded):
            return clean_location_piece(line)[:120]
        if 3 <= len(line) <= 120 and "bestelling " in folded:
            return clean_location_piece(re.sub(r"(?i)\bbestelling\b", "", line)).strip()[:120]
    return None


def vinted_extract_other_party(node: Any) -> str | None:
    for candidate in iter_dicts(node):
        for key in ("other_user", "user", "seller", "buyer"):
            user = candidate.get(key)
            if isinstance(user, dict):
                login = vinted_as_str(user.get("login") or user.get("username") or user.get("name"))
                if login:
                    return login
    return None


def vinted_other_party_from_text(text: str) -> str | None:
    match = re.search(r"\b(?:verkoper|seller)\s+([A-Za-z0-9_.-]{3,40})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    for line in meaningful_lines(text)[:8]:
        candidate = line.strip()
        folded = fold_text(candidate)
        if folded in {"vinted", "pakket volgen", "trackingnummer", "aankoop geslaagd"}:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,39}", candidate):
            return candidate
    return None


def vinted_carrier_tracking_from_values(
    *,
    carrier: str | None,
    tracking_code: str | None,
    text: str,
) -> dict[str, str]:
    text_reference = vinted_carrier_tracking("\n".join(value for value in (carrier or "", tracking_code or "", text) if value))
    if text_reference:
        if not text_reference.get("tracking_url"):
            tracking_url = tracking_url_for_vinted_carrier(text_reference["carrier"], text_reference["tracking_code"])
            if tracking_url:
                text_reference["tracking_url"] = tracking_url
        return text_reference
    normalized = normalize_vinted_carrier(carrier)
    code = re.sub(r"[^A-Z0-9]", "", str(tracking_code or "").upper())
    if not normalized or not code:
        return {}
    reference = {"carrier": normalized, "tracking_code": code}
    tracking_url = tracking_url_for_vinted_carrier(normalized, code)
    if tracking_url:
        reference["tracking_url"] = tracking_url
    return reference


def normalize_vinted_carrier(value: str | None) -> str:
    folded = fold_text(value or "")
    if "chrono" in folded:
        return "chronopost"
    if "postnl" in folded or "post nl" in folded:
        return "postnl"
    if "dhl" in folded:
        return "dhl"
    if "gls" in folded:
        return "gls"
    if "fedex" in folded or "fed ex" in folded:
        return "fedex"
    if "dpd" in folded:
        return "dpd"
    if "ups" in folded:
        return "ups"
    return ""


def tracking_url_for_vinted_carrier(carrier: str, tracking_code: str) -> str:
    encoded = quote_plus(tracking_code)
    if carrier == "chronopost":
        return CHRONOPOST_TRACKING_PAGE.format(tracking_code=encoded)
    if carrier == "dhl":
        return f"https://www.dhl.com/nl-nl/home/tracking/tracking-parcel.html?submit=1&tracking-id={encoded}"
    if carrier == "postnl":
        return f"https://jouw.postnl.nl/track-and-trace/{encoded}"
    if carrier == "gls":
        return f"https://gls-group.eu/NL/nl/pakket-volgen?match={encoded}"
    if carrier == "dpd":
        return f"https://www.dpd.com/nl/nl/ontvangen/volgen/?parcelNumber={encoded}"
    if carrier == "fedex":
        return FEDEX_TRACKING_PAGE.format(tracking_code=encoded)
    return ""


def vinted_api_status_text(package: dict[str, Any], *, status: str) -> str:
    title = vinted_as_str(package.get("item_title"))
    carrier = vinted_as_str(package.get("carrier"))
    tracking_code = vinted_as_str(package.get("tracking_code"))
    expected = vinted_expected_text(package.get("expected_date"), package.get("expected_date_to"))
    parts = [title, expected, carrier, tracking_code]
    compact = " - ".join(part for part in parts if part)
    if compact:
        return compact[:220]
    return status.replace("_", " ").title()


def vinted_expected_text(start: Any, end: Any) -> str:
    start_text = vinted_as_str(start)
    end_text = vinted_as_str(end)
    if start_text and end_text and start_text != end_text:
        return f"verwacht {start_text} t/m {end_text}"
    if start_text:
        return f"verwacht {start_text}"
    return ""


def vinted_records_from_json(
    payload: Any,
    *,
    account_key: str,
    source_url: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in iter_dicts(payload):
        text = " ".join(flatten_strings(item, limit=80))
        if not looks_like_vinted_parcel_text(text):
            continue
        record = vinted_record_from_text(
            text,
            account_key=account_key,
            source_url=source_url,
            vinted_id=vinted_payload_id(item),
        )
        if record:
            records.append(record)
    return dedupe_vinted_records(records)


def iter_dicts(value: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            results.append(item)
            for nested in item.values():
                walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)

    walk(value)
    return results


def vinted_payload_id(item: dict[str, Any]) -> str:
    for key in ("id", "transaction_id", "order_id", "shipment_id", "conversation_id"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def vinted_record_from_text(
    text: str,
    *,
    account_key: str,
    source_url: str,
    vinted_id: str = "",
) -> dict[str, Any] | None:
    if not looks_like_vinted_parcel_text(text):
        return None

    folded = fold_text(text)
    status = vinted_status_from_text(folded)
    pickup_code = vinted_pickup_code(text)
    pickup_location = vinted_pickup_location(text) if status == "ready_for_pickup" else ""
    carrier_reference = vinted_carrier_tracking(text)
    expected_date, expected_date_to = vinted_expected_delivery_range({}, text)
    deadline = vinted_pickup_deadline(text)
    item_title = vinted_item_title_from_text(text)
    other_party = vinted_other_party_from_text(text)
    tracking_events = vinted_tracking_events_from_text(text)

    if pickup_code and status in {"unknown", "in_transit", "expected_today"}:
        status = "ready_for_pickup"
    if status == "unknown" and not carrier_reference and not expected_date and not pickup_code and not pickup_location:
        return None

    tracking_code = (
        carrier_reference.get("tracking_code")
        if carrier_reference
        else vinted_id or vinted_tracking_code_from_text(text) or vinted_stable_text_id(text)
    )
    record: dict[str, Any] = {
        "carrier": "vinted",
        "shop": "Vinted",
        "tracking_code": tracking_code,
        "status": status,
        "source": "vinted_sidecar",
        "confidence": "high",
        "tracking_url": carrier_reference.get("tracking_url") if carrier_reference else source_url,
        "tracking_status_text": vinted_status_text(text, status=status),
        "tracking_refresh_source": "vinted_sidecar",
        "tracking_refresh_supported": True,
        "extra": {
            "vinted_account": account_key,
            "vinted_source_url": source_url,
        },
    }
    if expected_date and status not in {"ready_for_pickup", "picked_up", "delivered", "cancelled"}:
        record["expected_date"] = expected_date
    if expected_date_to:
        record["extra"]["expected_date_end"] = expected_date_to
        record["extra"]["vinted_expected_date_to"] = expected_date_to
    if item_title:
        record["extra"]["vinted_item_title"] = item_title
    if other_party:
        record["extra"]["vinted_other_party"] = other_party
    if tracking_events:
        record["extra"]["tracking_events"] = tracking_events[:10]
    if pickup_location:
        record["pickup_location"] = pickup_location
    if pickup_code:
        record["pickup_code"] = pickup_code
    if deadline:
        record["extra"]["pickup_deadline"] = deadline
    if vinted_id:
        record["extra"]["vinted_id"] = vinted_id
    if carrier_reference:
        record["extra"]["carrier_tracking"] = carrier_reference
    return record


def looks_like_vinted_parcel_text(text: str) -> bool:
    folded = fold_text(text)
    return any(
        term in folded
        for term in (
            "pakket",
            "parcel",
            "zending",
            "shipment",
            "tracking",
            "track",
            "ready for pickup",
            "afhaalcode",
            "ophaalcode",
            "pickup code",
            "qr-code",
            "qr code",
            "ligt klaar",
            "ready for pickup",
            "ophalen",
            "pickup point",
            "pakketpunt",
            "point relais",
            "chronopost",
            "dhl",
            "postnl",
        )
    )


def vinted_status_from_text(folded: str) -> str:
    if any(term in folded for term in ("opgehaald", "afgehaald", "picked up", "collected by buyer")):
        return "picked_up"
    if any(
        term in folded
        for term in (
            "ligt klaar",
            "klaar om opgehaald",
            "ready for pickup",
            "available for pickup",
            "afhaalcode",
            "ophaalcode",
            "pickup code",
            "qr-code om je pakket op te halen",
            "qr code to pick up",
        )
    ):
        return "ready_for_pickup"
    if any(term in folded for term in ("wordt vandaag bezorgd", "out for delivery", "bezorging vandaag")):
        return "expected_today"
    if any(term in folded for term in ("bezorgd", "afgeleverd", "delivered")):
        return "delivered"
    if any(term in folded for term in ("verzonden", "onderweg", "in transit", "shipped", "on its way")):
        return "in_transit"
    if any(term in folded for term in ("geannuleerd", "cancelled", "canceled")):
        return "cancelled"
    return "unknown"


def vinted_pickup_code(text: str) -> str:
    patterns = (
        r"\b(?:afhaalcode|ophaalcode|pickup code|code)\b\D{0,40}([A-Z0-9]{4,12})\b",
        r"\bvul deze code in\D{0,30}([A-Z0-9]{4,12})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            code = re.sub(r"[^A-Z0-9]", "", match.group(1).upper())
            if 4 <= len(code) <= 12:
                return code
    return ""


def vinted_pickup_location(text: str) -> str:
    compact = re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()
    patterns = (
        r"\bligt klaar bij\s+(.+?)(?:\b(?:afhaalcode|ophaalcode|code|openingstijden|trackingnummer)\b|[.]\s|$)",
        r"\b(?:adres|afhaalpunt|ophaalpunt|pakketpunt|pickup point|point relais)\s*[:\-]\s*(.+?)(?:\b(?:afhaalcode|ophaalcode|code|openingstijden|trackingnummer)\b|[.]\s|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            location = clean_vinted_location(match.group(1))
            if location:
                return location

    lines = meaningful_lines(text)
    for line in lines:
        folded = fold_text(line)
        if any(term in folded for term in ("pakketwinkel", "pakketpunt", "pickup point", "point relais")):
            location = clean_vinted_location(line)
            if location:
                return location
    return ""


def clean_vinted_location(value: str) -> str:
    text = clean_location_piece(value)
    text = re.sub(r"\b(?:afhaalcode|ophaalcode|code|openingstijden|trackingnummer)\b.*$", "", text, flags=re.IGNORECASE)
    text = text.strip(" ,.;:-")
    if not text or len(text) < 4:
        return ""
    folded = fold_text(text)
    if folded in {"vinted", "vinted go", "pakketpunt", "pickup point"}:
        return ""
    return text[:180]


def vinted_pickup_deadline(text: str) -> str:
    match = re.search(r"\b(?:ophalen voor|ophalen vóór|pick up by)\D{0,30}(\d{1,2}[-/]\d{1,2}[-/]\d{4})", text, re.IGNORECASE)
    return date_from_any(match.group(1)) if match else ""


def vinted_tracking_code_from_text(text: str) -> str:
    match = re.search(r"\b(?:trackingnummer|tracking number|zendingsnummer|shipment number)\D{0,30}([A-Z0-9]{8,40})\b", text, re.IGNORECASE)
    return re.sub(r"[^A-Z0-9]", "", match.group(1).upper()) if match else ""


def vinted_carrier_tracking(text: str) -> dict[str, str]:
    urls = extract_urls(text)
    folded = fold_text(text)
    patterns = (
        ("chronopost", r"\bXU[A-Z0-9]{8,24}\b", ("chronopost",)),
        ("dhl", r"\b(?:JJD[A-Z0-9]{12,32}|3S[A-Z0-9]{8,24})\b", ("dhl", "dhlecommerce", "dhl parcel")),
        ("postnl", r"\b3S[A-Z0-9]{8,24}\b", ("postnl", "post nl")),
        ("gls", r"\b[A-Z0-9]{8,16}\b", ("gls",)),
        ("dpd", r"\b[A-Z0-9]{10,24}\b", ("dpd",)),
    )
    for carrier, pattern, keywords in patterns:
        if not any(keyword in folded for keyword in keywords):
            continue
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        code = re.sub(r"[^A-Z0-9]", "", match.group(0).upper())
        reference = {"carrier": carrier, "tracking_code": code}
        for url in urls:
            if code.lower() in url.lower() or carrier in url.lower():
                reference["tracking_url"] = url
                break
        return reference
    return {}


def extract_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(").,;\"'") for match in re.finditer(r"https?://\S+", str(text or ""))]


def vinted_status_text(text: str, *, status: str) -> str:
    for line in meaningful_lines(text):
        if status == "ready_for_pickup" and any(
            term in fold_text(line) for term in ("ligt klaar", "ready for pickup", "afhaalcode", "pickup point")
        ):
            return line[:220]
        if status != "unknown" and status.replace("_", " ") in fold_text(line):
            return line[:220]
    return clean_location_piece(text)[:220]


def vinted_stable_text_id(text: str) -> str:
    digest = hashlib.sha1(clean_location_piece(text).encode("utf-8")).hexdigest()[:16]
    return f"vinted-{digest}"


def dedupe_vinted_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        key = vinted_record_key(record)
        existing = deduped.get(key)
        if existing is None or vinted_record_score(record) >= vinted_record_score(existing):
            deduped[key] = record
    return list(deduped.values())


def vinted_record_key(record: dict[str, Any]) -> str:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    reference = extra.get("carrier_tracking") if isinstance(extra.get("carrier_tracking"), dict) else {}
    carrier = reference.get("carrier")
    code = reference.get("tracking_code")
    if carrier and code:
        return f"{carrier}:{code}".lower()
    if extra.get("vinted_id"):
        return f"vinted:{extra['vinted_id']}"
    return f"vinted:{record.get('tracking_code') or vinted_stable_text_id(str(record))}".lower()


def vinted_record_score(record: dict[str, Any]) -> tuple[int, int]:
    status = str(record.get("status") or "unknown")
    status_score = {
        "picked_up": 50,
        "delivered": 45,
        "ready_for_pickup": 40,
        "expected_today": 30,
        "in_transit": 20,
        "unknown": 0,
    }.get(status, 0)
    detail_score = sum(1 for key in ("pickup_location", "pickup_code", "expected_date", "tracking_url") if record.get(key))
    return (status_score, detail_score)


def redact_vinted_debug_text(text: str) -> str:
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "EMAIL", str(text or ""))
    value = re.sub(r"\b[A-Z0-9]{8,40}\b", "CODE", value)
    value = re.sub(r"\b\d{6,}\b", "NUMBER", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


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
    app.router.add_post("/login/vinted/session", vinted_session_update)
    app.router.add_get("/parcels/vinted", vinted_parcels)
    app.router.add_post("/parcels/vinted", vinted_parcels)
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
