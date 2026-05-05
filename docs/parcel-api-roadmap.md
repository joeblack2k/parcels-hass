# Parcel API Roadmap

The integration is local-first today. Parcel REST API support should be optional and additive.

## References

- Official Parcel API docs: https://parcelapp.net/help/api-view-deliveries.html
- Existing community integration: https://github.com/jmdevita/parcel-ha

## V2 scope

- Config entry or YAML option for an API key.
- Raw data sensor for debugging.
- Status-code mapping through `custom_components/package_inbox/parcel_api.py`.
- Add, edit, and delete delivery services where the API allows it.
- Supported-carrier registry and carrier aliases.
- Dedupe against IMAP, PostNL, and public tracking sources.

## Local-first carrier baseline

The public integration should prefer IMAP/mail parsing first, then public carrier tracking pages or APIs when they are available without account secrets.

Current Netherlands-oriented carrier targets:

| Carrier | Parcel/app code hints | Baseline |
| --- | --- | --- |
| PostNL | `tntp`, `tntpit` | IMAP, PostNL HA sensor, postcode-aware public tracking |
| DHL Netherlands | `dhlnl`, `dhlnlpcode` | IMAP, public DHL page/API |
| GLS Netherlands | `glsnl` | IMAP, public tracking URL |
| Trunkrs | `trnkrpcode` | IMAP, postcode-aware public tracking URL |
| Homerr | `homerr` | IMAP, pickup/tracking code extraction, public tracking page |
| Cycloon | `cyclpcode` | IMAP, public tracking page |
| Instabox / Red je pakketje | `redjep` | IMAP, public tracking page |
| TransMission | `transm` | IMAP, public tracking page/API research |
| Dachser | `dachser` | IMAP, public tracking page |
| Dynalogic | `dynalogic` | IMAP, postcode/house-number-aware public URL |
| UPS | `ups` | IMAP, public tracking URL |
| Amazon Netherlands | `amzlnl` | IMAP only; no cookies or account scraping in public flow |
| Vinted Go | `vinted` | IMAP/pickup flow; public tracking page where mail provides enough data |

Do not mark a carrier as fully supported unless parser fixtures and tracking-refresh behavior are covered by tests.

## Status-code mapping

| Parcel code | Parcel label | Internal status |
| --- | --- | --- |
| 0 | completed | delivered |
| 1 | frozen | unknown |
| 2 | in_transit | in_transit |
| 3 | pickup | ready_for_pickup |
| 4 | out_for_delivery | expected_today |
| 5 | not_found | unknown |
| 6 | failed | unknown |
| 7 | exception | unknown |
| 8 | info_received | in_transit |

The internal model does not yet have dedicated `failed`, `exception`, or `frozen` statuses. Keep the Parcel label in raw attributes when the REST source is implemented.
