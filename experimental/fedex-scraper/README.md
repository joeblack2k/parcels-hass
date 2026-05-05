# Experimental FedEx Scraper Sidecar

This is a private/personal-use helper for `parcels-hass`. It is not part of the default HACS integration.

The sidecar uses Playwright to open the FedEx tracking page locally, capture FedEx tracking JSON when the browser receives it, and return only normalized parcel fields to Home Assistant.

No FedEx account credentials, browser cookies, raw HTML, or session tokens are sent to Home Assistant by design.

## Run

```bash
docker compose up -d
```

Optional bearer token:

```yaml
services:
  fedex-scraper:
    environment:
      SCRAPER_TOKEN: "long-random-token"
```

## Home Assistant YAML

```yaml
package_inbox:
  tracking_scraper_url: "http://YOUR_HOST:8765"
  tracking_scraper_token: !secret parcels_tracking_scraper_token
```

If the sidecar is unavailable or FedEx blocks the browser session, Parcels falls back to the normal IMAP/public tracking path.

## API

Health:

```bash
curl http://localhost:8765/health
```

Track:

```bash
curl -X POST http://localhost:8765/track \
  -H 'Content-Type: application/json' \
  -d '{"carrier":"fedex","tracking_code":"123456789012"}'
```

With token:

```bash
curl -X POST http://localhost:8765/track \
  -H 'Authorization: Bearer long-random-token' \
  -H 'Content-Type: application/json' \
  -d '{"carrier":"fedex","tracking_code":"123456789012"}'
```

## Security Notes

- Bind it to your LAN only.
- Prefer `SCRAPER_TOKEN` if the port is reachable beyond localhost.
- Do not expose it to the public internet.
- Do not log request bodies or tracking URLs at debug level on shared systems.
- Treat this as an experimental workaround for personal inbound parcel tracking, not a public carrier API.
