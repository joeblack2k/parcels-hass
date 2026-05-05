# Parcels FedEx Scraper

Experimental Home Assistant OS app/add-on for personal FedEx tracking refreshes in Parcels for Home Assistant.

FedEx can block normal server-side HTTP tracking with Akamai permission pages. This app runs a local Playwright browser inside Home Assistant OS, opens FedEx tracking locally, captures FedEx tracking JSON when available, and returns only normalized parcel status to the `package_inbox` integration.

It does not send FedEx cookies, raw HTML, browser storage, or account tokens back to Home Assistant.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `scraper_token` | not set | Optional bearer token expected from the integration. Use this if you expose the port. |
| `headless` | `true` | Runs Chromium headless. |
| `timeout` | `45` | FedEx page/API wait timeout in seconds. |

## Home Assistant YAML

For a local add-on install, use the Supervisor internal hostname:

```yaml
package_inbox:
  tracking_scraper_url: "http://local-parcels-fedex-scraper:8765"
  tracking_scraper_token: !secret parcels_tracking_scraper_token
```

If you expose port `8765` in the add-on network settings, you can also use your Home Assistant host/IP instead of the internal hostname.

Keep this app on your LAN and do not expose it to the public internet.
