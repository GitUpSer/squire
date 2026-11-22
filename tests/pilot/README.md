# Browser acceptance harness

`run.py` starts real control-api, its provisioning/lifecycle worker, messaging
API and messaging worker as separate processes. It loads the production Caddy
configuration, substituting only the static asset path and upstream environment.
It uses separate PostgreSQL schemas in an explicitly disposable `_test` database.
Nothing reaches real WhatsApp, Railway, email or model endpoints.

The HTTP fixtures simulate those external services, and a runtime contract
simulator accepts durable event IDs and replies through real scoped egress.
The simulator is an explicit boundary: this test does **not** validate Hermes
execution, real WhatsApp pairing/delivery, provider receipts, or founder-memory
migration. The `/events` fixture can be replaced by the actual runtime adapter
without changing browser/customer tests once that adapter is available.

Requirements: Python environments containing the installed control and messaging
apps, PostgreSQL, Caddy, Node and Playwright Chromium. Configuration:

```
PILOT_TEST_DATABASE_URL=postgresql://.../squire_integration_test
CONTROL_PYTHON=/path/to/control-env/bin/python
MESSAGING_PYTHON=/path/to/messaging-env/bin/python
CADDY_BIN=/path/to/caddy
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright
CHROMIUM_BIN=/path/to/chromium
PILOT_TEST_ARTIFACTS=/tmp/squire-pilot-acceptance
```

Run `CONTROL_PYTHON tests/pilot/run.py` from the repo root. It owns ports
18740–18744 and always terminates child processes. It destructively recreates
only schemas `pilot_control` and `pilot_messaging` in the supplied test database.
Fixtures use generated temporary local storage. Test credentials are static,
public values and must never be used in production.

The browser flow verifies two account signups, a first outbound WhatsApp code
before any incoming WhatsApp message, phone verification, real provisioning
steps, heartbeat-gated intro, first-turn trial/model activation, duplicate replay,
cross-customer recipient denial, group owner and LID-administrator approval,
independent group runtime scope, addressed-only group replies, owner approval
with a separate selected-result release, account pause, mobile overflow, and
JavaScript errors. JSON report and desktop/mobile screenshots are saved to the
artifact directory. Service logs contain only synthetic fixture data.

The same suite runs in CI alongside PostgreSQL service tests and image builds.
CI does not deploy or send real messages.
