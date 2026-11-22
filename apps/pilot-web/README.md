# Squire pilot web

Static, CSP-restricted same-origin customer UI. Build with the included Dockerfile
and deploy separately from the existing marketing site. Set `PORT` and
`CONTROL_API_UPSTREAM=control-api.railway.internal:8080`; Caddy strips `/api` and
proxies to control-api. Configure control's `PILOT_WEB_ORIGIN` to the exact HTTPS
public origin. Cookies remain host-only and HttpOnly. No browser service secrets.

Invitation links use a URL fragment, removed immediately and stored only for the
current browser session, to avoid sending the invitation to access logs/referrers.
The sign-in email must match the invitation. UI states come from `/v1/me` and real
control APIs; no demo mode or simulated WhatsApp success is present.

A successful web build is not an end-to-end delivery test. Production acceptance
requires email delivery, WhatsApp challenge, runtime readiness, outbound welcome,
first customer reply, trial activation, scoped group approval and STOP testing.
