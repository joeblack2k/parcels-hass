"""Local FedEx tracking scraper sidecar for personal parcels-hass setups."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import html
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote_plus

from aiohttp import web
from playwright.async_api import Browser, Error as PlaywrightError, async_playwright

LOG = logging.getLogger("parcels_fedex_scraper")

ADDON_OPTIONS_PATH = Path("/data/options.json")
FEDEX_HOME = "https://www.fedex.com/en-us/home.html"
FEDEX_TRACKING_PAGE = "https://www.fedex.com/fedextrack/?trknbr={tracking_code}"
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
)


@dataclass(slots=True)
class Settings:
    host: str
    port: int
    token: str
    headless: bool
    timeout: int


def settings_from_env() -> Settings:
    addon_options = load_addon_options()
    return Settings(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=parse_int(os.environ.get("PORT"), parse_int(addon_options.get("port"), 8765)),
        token=os.environ.get("SCRAPER_TOKEN") or str(addon_options.get("scraper_token") or ""),
        headless=parse_bool(
            os.environ.get("FEDEX_SCRAPER_HEADLESS"),
            parse_bool(addon_options.get("headless"), True),
        ),
        timeout=parse_int(
            os.environ.get("FEDEX_SCRAPER_TIMEOUT"),
            parse_int(addon_options.get("timeout"), 45),
        ),
    )


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


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "parcels-fedex-scraper"})


async def track(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception as err:
        raise web.HTTPBadRequest(text="invalid json") from err

    carrier = str(payload.get("carrier") or "fedex").strip().lower()
    tracking_code = normalize_tracking_code(payload.get("tracking_code"))
    if carrier != "fedex":
        raise web.HTTPBadRequest(text="only fedex is supported by this sidecar")
    if not tracking_code:
        raise web.HTTPBadRequest(text="missing tracking_code")

    tracking_url = str(payload.get("tracking_url") or "").strip() or FEDEX_TRACKING_PAGE.format(
        tracking_code=quote_plus(tracking_code)
    )
    timeout = request.app["settings"].timeout
    result = await scrape_fedex(
        request.app["browser"],
        tracking_code=tracking_code,
        tracking_url=tracking_url,
        timeout=timeout,
    )
    return web.json_response(result)


def normalize_tracking_code(value: Any) -> str:
    code = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return code if 10 <= len(code) <= 40 else ""


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
            content = await page.content()
            update = normalize_fedex_html(content, tracking_code=tracking_code, tracking_url=tracking_url)
            return update
    except PlaywrightError as err:
        return error_update(tracking_code, tracking_url, f"playwright_error: {err}")
    finally:
        await context.close()


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


def error_update(tracking_code: str, tracking_url: str | None, error: str) -> dict[str, Any]:
    update = {
        "carrier": "fedex",
        "tracking_code": tracking_code,
        "status": "unknown",
        "tracking_refresh_source": "local_tracking_scraper",
        "tracking_refresh_supported": True,
        "tracking_refresh_error": error,
    }
    if tracking_url:
        update["tracking_url"] = tracking_url
    return update


async def start_browser(app: web.Application) -> None:
    settings = app["settings"]
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=settings.headless)
    app["playwright"] = playwright
    app["browser"] = browser


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
    app.router.add_get("/health", health)
    app.router.add_post("/track", track)
    app.on_startup.append(start_browser)
    app.on_cleanup.append(stop_browser)
    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    settings = settings_from_env()
    web.run_app(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
