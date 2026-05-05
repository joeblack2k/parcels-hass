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
