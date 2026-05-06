# Parcels Tracking Scraper

Experimental Home Assistant OS app/add-on for personal tracking refreshes in Parcels for Home Assistant.

Some carriers block normal server-side HTTP tracking or render the useful details in the browser. This app runs a local Playwright browser inside Home Assistant OS, opens the tracking page locally, captures tracking JSON when available, and returns only normalized parcel status to the `package_inbox` integration.

It does not send carrier cookies, raw HTML, browser storage, or account tokens back to Home Assistant.

Supported carriers:

- FedEx
- Chronopost
- Vinted login/session refresh (experimental; tracking enrichment follows later)

## Options

| Option | Default | Description |
| --- | --- | --- |
| `scraper_token` | not set | Optional bearer token expected from the integration. Use this if you expose the port. |
| `headless` | `true` | Runs Chromium headless. |
| `timeout` | `45` | Browser request timeout in seconds. |
| `vinted_auto_login` | `false` | Periodically refreshes a Vinted browser session when credentials are configured. |
| `vinted_login_on_start` | `true` | Runs one Vinted login refresh when the add-on starts. |
| `vinted_login_interval_hours` | `22` | Delay between automatic Vinted login refreshes. |
| `vinted_email` | not set | First Vinted account e-mail; keep this local in the add-on options. |
| `vinted_password` | not set | First Vinted password; stored by Home Assistant as a password option. |
| `vinted_email_2` | not set | Optional second Vinted account e-mail. |
| `vinted_password_2` | not set | Optional second Vinted password; stored by Home Assistant as a password option. |

## Vinted Auto Login

Vinted sessions can expire quickly. When `vinted_auto_login` is enabled and
credentials are present, the add-on opens a persistent Chromium profile under
`/data/browser-profiles/vinted`, submits the Vinted login form, stores only the
result status, and closes the browser context again. When two accounts are
configured, each account uses a separate profile under that directory so cookies
and login state do not overwrite each other. It does not try to bypass captcha,
two-factor prompts, suspicious-login checks, or account challenges; it returns a
clear status such as `captcha_required`, `two_factor_required`, or
`login_required`.

Useful endpoints:

- `GET /login/vinted/status`
- `POST /login/vinted`

POST without a body refreshes all configured accounts. To refresh one account,
send `{"account": "account_1"}` or `{"account": "account_2"}`.

Both endpoints are protected by `scraper_token` when you configure one. Keep
this add-on on your LAN and do not expose it to the public internet.

## Home Assistant YAML

For a local add-on install, use the Supervisor internal hostname:

```yaml
package_inbox:
  tracking_scraper_url: "http://local-parcels-fedex-scraper:8765"
  tracking_scraper_token: !secret parcels_tracking_scraper_token
```

If you expose port `8765` in the add-on network settings, you can also use your Home Assistant host/IP instead of the internal hostname.

Keep this app on your LAN and do not expose it to the public internet.
