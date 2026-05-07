# Parcels Tracking Scraper

Experimental Home Assistant OS app/add-on for personal tracking refreshes in Parcels for Home Assistant.

Some carriers block normal server-side HTTP tracking or render the useful details in the browser. This app runs a local Playwright browser inside Home Assistant OS, opens the tracking page locally, captures tracking JSON when available, and returns only normalized parcel status to the `package_inbox` integration.

It does not send carrier cookies, raw HTML, browser storage, or account tokens back to Home Assistant.

Supported carriers:

- FedEx
- Chronopost
- Vinted login/session refresh and normalized parcel mirroring (experimental)

## Options

| Option | Default | Description |
| --- | --- | --- |
| `scraper_token` | not set | Optional bearer token expected from the integration. Use this if you expose the port. |
| `headless` | `true` | Runs Chromium headless. |
| `timeout` | `45` | Browser request timeout in seconds. |
| `vinted_auto_login` | `false` | Periodically refreshes a Vinted browser session when credentials are configured. |
| `vinted_login_on_start` | `true` | Runs one Vinted login refresh when the add-on starts. |
| `vinted_login_interval_hours` | `6` | Delay between automatic Vinted login/session refreshes. |
| `vinted_browser_ui` | `false` | Starts an optional Xvfb/noVNC browser desktop for manual Vinted profile login. |
| `vinted_browser_ui_password` | not set | Optional VNC password for the manual browser desktop. |
| `vinted_email` | not set | First Vinted account e-mail; keep this local in the add-on options. |
| `vinted_password` | not set | First Vinted password; stored by Home Assistant as a password option. |
| `vinted_session_cookie` | not set | Optional first-account Vinted browser session cookie. Prefer this when Vinted blocks password auth. |
| `vinted_email_2` | not set | Optional second Vinted account e-mail. |
| `vinted_password_2` | not set | Optional second Vinted password; stored by Home Assistant as a password option. |
| `vinted_session_cookie_2` | not set | Optional second-account Vinted browser session cookie. Prefer this when Vinted blocks password auth. |

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

After a successful browser login or an already logged-in browser profile, the
add-on stores a sanitized Vinted session cookie locally under
`/data/vinted_sessions.json` and uses it for the lighter API-first parcel mirror.
When Vinted refreshes an access token, the refreshed session is written back to
that same local store. Status and health endpoints expose only safe metadata
such as cookie names and timestamps, never cookie values or account tokens.

The parcel mirror uses a lighter API-first flow before it opens Chromium. If
Vinted rejects password auth, you can provide a local `vinted_session_cookie`
captured from an already logged-in browser profile. The add-on accepts only
Vinted session/refresh cookies, refreshes `access_token_web` locally when
possible, and never returns cookie values in `/health`, `/login/vinted/status`,
or `/parcels/vinted`.

For private setups or browser-extension bridges, `POST /login/vinted/session`
accepts `{"account": "account_1", "cookie": "..."}` or a Chrome-style
`cookies` list. It stores only a sanitized Vinted cookie string in
`/data/vinted_sessions.json` and marks that account usable for API refreshes.

When Vinted blocks automated password login, enable `vinted_browser_ui`, map
port `6080`, and open `POST /browser/vinted/open` for the account you want to
repair. Log in through noVNC, then call `POST /browser/vinted/close`; the add-on
stores the refreshed profile cookies locally and the normal parcel mirror can use
that same profile. The browser UI is intended for private LAN use only.

Useful endpoints:

- `GET /login/vinted/status`
- `POST /login/vinted`
- `POST /login/vinted/session`
- `GET /browser/vinted/status`
- `POST /browser/vinted/open`
- `POST /browser/vinted/close`
- `GET /parcels/vinted`
- `POST /parcels/vinted`

POST without a body refreshes all configured accounts. To refresh one account,
send `{"account": "account_1"}` or `{"account": "account_2"}`.

`/parcels/vinted` opens the already logged-in local profiles, reads Vinted
shipment/order pages and browser JSON responses, and returns only normalized
parcel records. It does not return raw page text, cookies, browser storage, or
account tokens. When Vinted shows a carrier tracking reference, the response
keeps `carrier: vinted` and includes `extra.carrier_tracking` so the Home
Assistant integration can merge Vinted truth into the carrier package key. For
richer dashboards it also mirrors safe parcel details such as item title, other
party username, expected delivery date range, and tracking timeline events.

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
