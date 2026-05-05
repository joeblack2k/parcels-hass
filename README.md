# Parcels for Home Assistant (`parcels-hass`)

Privacy-first parcel inbox for Home Assistant. Parcels watches local mail and Home Assistant data, normalizes parcel records, enriches supported public tracking pages, and exposes dashboard-friendly sensors and services.

The public project name is **Parcels for Home Assistant**. The Home Assistant integration domain intentionally remains `package_inbox` for compatibility with existing entities, automations, YAML, storage, and services.

## What it does

- Parses parcel mail from IMAP events.
- Tracks PostNL, DHL, DPD, GLS, FedEx, Chronopost, UPS, Trunkrs, Homerr, Cycloon, Instabox/Red je pakketje, TransMission, Dachser, Dynalogic, GOFO, Dragonfly, Amazon, Vinted, and pickup-style messages where enough data is present.
- Enriches public tracking pages and mail-provided tracking links on a best-effort basis. Postcode and house-number settings are used only locally for carriers that need receiver address verification.
- Uses the PostNL Home Assistant delivery sensor when configured.
- Exposes dashboard sensors for active parcels, due-today parcels, pickup parcels, and delivery windows.
- Provides services to add, edit, refresh, mark picked up, delete, and debug package records.
- Can send optional Home Assistant/Matrix notifications when configured locally.

## Install

For HACS custom repository testing:

1. Add `https://github.com/joeblack2k/parcels-hass` as a custom repository.
2. Select category `Integration`.
3. Install **Parcels for Home Assistant**.
4. Restart Home Assistant.
5. Configure the `package_inbox:` YAML block.

Minimal YAML:

```yaml
package_inbox:
  imap_entry_id: YOUR_IMAP_ENTRY_ID
  notify_script: persistent_notification.create
  enable_event_listener: true
  enable_tracking_refresh: true
```

Optional local hooks:

```yaml
package_inbox:
  imap_entry_id: YOUR_IMAP_ENTRY_ID
  notify_script: script.your_package_notification_script
  matrix_room_id: ""
  delivery_postcode: ""
  delivery_house_number: ""
  postnl_delivery_sensor: sensor.postnl_delivery
  public_qr_dir: package_inbox
  tracking_refresh_minutes: 60
  tracking_scraper_url: ""
  tracking_scraper_token: ""
```

For postcode-gated carrier tracking, set your local receiver details in Home Assistant YAML:

```yaml
package_inbox:
  delivery_postcode: "1234AB"
  delivery_house_number: "12"
```

Do not put personal receiver details in public bug reports or shared test fixtures.

## Experimental local scraper

For personal setups where FedEx blocks server-side tracking requests, Parcels can optionally call a local scraper sidecar before falling back to IMAP/public tracking. This is disabled by default and not required for HACS use.

See [docs/fedex-scraper.md](docs/fedex-scraper.md) and [experimental/fedex-scraper](experimental/fedex-scraper).

## Services

The service namespace is `package_inbox`.

- `package_inbox.process_imap_event`
- `package_inbox.debug_parse`
- `package_inbox.add_package`
- `package_inbox.set_status`
- `package_inbox.delete_package`
- `package_inbox.mark_picked_up`
- `package_inbox.refresh_tracking`
- `package_inbox.send_morning_summary`
- `package_inbox.send_pickup_summary`

## Compatibility

This repository uses the new public package name `parcels-hass`, but the integration keeps the legacy `package_inbox` domain on purpose. A future `parcels_hass` domain would be a breaking release and needs a migration path for entity IDs, storage keys, services, and YAML.

## Parcel app parity roadmap

V1 focuses on the current local-first flow:

- IMAP/mail parsing.
- PostNL Home Assistant delivery sensor support.
- Public tracking enrichment for supported carriers.
- Postcode-aware tracking URLs for carriers that expose that publicly.
- Manual add, edit, status update, delete, and pickup summaries.
- Dashboard sensors and Matrix/Home Assistant notification hooks.

V2 adds optional Parcel REST API parity:

- API-key source for Parcel app users.
- Raw data sensor for debugging.
- Status-code mapping.
- Add, edit, and delete deliveries.
- Supported-carrier registry.

V3 improves the everyday experience:

- Richer delivery event timelines.
- Carrier icons.
- Better Amazon and Vinted pickup flows.
- Import/export.
- Dedupe across IMAP, public tracking, PostNL, and Parcel API sources.

## Amazon support

The public baseline is mail parsing only: shipped, out-for-delivery, delivered, pickup/locker, return filtering, and order-only filtering. Amazon cookies, session tokens, and account scraping are not part of the default public HACS flow.

The browser cookie bridge idea is tracked as a separate experimental/private spike in [docs/amazon-cookiebridge-threat-model.md](docs/amazon-cookiebridge-threat-model.md).

## Disclaimer

This project is not affiliated with Parcel, Amazon, PostNL, DHL, DPD, GLS, FedEx, Chronopost, UPS, Trunkrs, Homerr, Cycloon, Instabox, TransMission, Dachser, Dynalogic, GOFO, Dragonfly, Vinted, Home Assistant, or HACS. Parcel REST API compatibility is planned as an optional source; see the official [Parcel API docs](https://parcelapp.net/help/api-view-deliveries.html) and the existing community integration [jmdevita/parcel-ha](https://github.com/jmdevita/parcel-ha) for prior art.

## Development

```bash
python -m pip install --upgrade pip pytest
pytest
python -m compileall -q custom_components/package_inbox
```

The repository intentionally excludes Home Assistant runtime data such as `.storage`, secrets, logs, databases, local dashboards, diagnostics, and personal defaults.
