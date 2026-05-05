# Amazon Cookie Bridge Threat Model

This is an experimental/private track, not part of the default public HACS flow.

## Public-safe baseline

Amazon support should start with mail parsing:

- Shipment created and shipped.
- Out for delivery.
- Delivered.
- Pickup, locker, or counter collection.
- Return accepted filtering.
- Order-only filtering.

No Amazon cookies, account tokens, or session material should be required for the public integration.

## Preferred experimental design

The safer design is a browser extension that reads Amazon pages locally and sends only normalized parcel records to Home Assistant:

```json
{
  "source": "amazon_extension",
  "carrier": "amazon",
  "shop": "Amazon",
  "status": "in_transit",
  "expected_date": "2026-05-05",
  "tracking_code": null,
  "tracking_url": null
}
```

The extension would need:

- Home Assistant URL or LAN IP.
- A dedicated Home Assistant long-lived token or webhook secret.
- A strict allowlist for the Home Assistant endpoint.
- Local redaction before any data leaves the browser.

Raw cookie forwarding to Home Assistant should exist only behind an explicit dangerous/private mode.

## Data that must never be logged or stored by default

- Raw Amazon cookies.
- Amazon session IDs.
- Browser tokens.
- Home Assistant tokens or webhook secrets.
- Full Amazon account pages.
- Payment, address, or account settings data.
- Raw diagnostics containing cookie headers.

These values must never be sent to Matrix, GitHub Actions logs, regular Home Assistant package storage, issue templates, or public diagnostics.

## Main risks

- Cookie theft gives account access.
- Home Assistant token leakage gives HA API access.
- LAN exposure can turn a local-only bridge into a remote attack surface.
- CSRF/CORS mistakes can let another page trigger extension or HA calls.
- Browser extension permissions can become too broad.
- Amazon sessions expire and create confusing partial data.
- Amazon terms or account-risk concerns may apply to scraping or automation.

## Minimum controls before a private spike

- Dedicated endpoint and secret, separate from normal HA auth.
- HTTPS or trusted LAN-only setup.
- No cookie persistence in Home Assistant.
- Redaction tests for logs, diagnostics, Matrix, and storage.
- Extension permissions limited to Amazon domains and the configured HA endpoint.
- Manual opt-in with warning text for any dangerous/private mode.
