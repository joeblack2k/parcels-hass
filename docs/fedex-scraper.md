# Experimental FedEx Scraper

FedEx's public tracking page can block server-side fetches with Akamai permission pages. For personal Home Assistant setups, `parcels-hass` can optionally ask a local Playwright sidecar for FedEx tracking status before falling back to IMAP/public tracking.

This is intentionally off by default and separate from the HACS integration.

## Architecture

```text
Home Assistant package_inbox
  POST /track {carrier, tracking_code}
        |
        v
local parcels-fedex-scraper sidecar
  Playwright browser opens FedEx locally
  captures FedEx JSON response when available
        |
        v
normalized status only: delivered / expected_today / in_transit / unknown
```

The sidecar does not send FedEx cookies, raw HTML, browser storage, or account tokens back to Home Assistant.

## Home Assistant OS Add-on

On Home Assistant OS, use the Supervisor-managed app/add-on instead of a loose Docker Compose service.

The add-on lives in this repository at:

```text
addons/parcels_fedex_scraper
```

For a local HAOS install, copy that folder into the Home Assistant `addons` share as `parcels_fedex_scraper`, reload the add-on store, install **Parcels FedEx Scraper**, and start it. The app exposes an internal HTTP API on port `8765`.

Local add-on URL for the integration:

```yaml
package_inbox:
  tracking_scraper_url: "http://local-parcels-fedex-scraper:8765"
  tracking_scraper_token: !secret parcels_tracking_scraper_token
```

The token is optional while the add-on is only reachable over the Supervisor internal network. If you expose port `8765` in the add-on network settings, set `scraper_token` in the add-on options and the same value in `tracking_scraper_token`.

## Standalone Docker Sidecar

From this repository:

```bash
cd experimental/fedex-scraper
docker compose up -d
```

Optional bearer token:

```yaml
services:
  fedex-scraper:
    environment:
      SCRAPER_TOKEN: "long-random-token"
```

## Configure Home Assistant

```yaml
package_inbox:
  imap_entry_id: YOUR_IMAP_ENTRY_ID
  tracking_scraper_url: "http://YOUR_SIDE_CAR_HOST:8765"
  tracking_scraper_token: !secret parcels_tracking_scraper_token
```

If the sidecar is unavailable, times out, or cannot get useful FedEx data, Parcels continues with the normal IMAP/public tracking path.

## Security Guidance

- Run only on your LAN.
- Use `SCRAPER_TOKEN` if the port is reachable by other devices.
- Do not expose the sidecar to the public internet.
- Do not store browser cookies or raw carrier responses in Home Assistant.
- Treat the sidecar as a personal workaround, not a public FedEx API.

## Current Scope

- FedEx only.
- Status, expected date, delivery window, latest status text, and a compact event list.
- No package management UI and no carrier account login.

Future carriers with heavy bot protection can use the same sidecar contract without changing the public Home Assistant entity model.
