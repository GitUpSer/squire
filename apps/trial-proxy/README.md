# Pilot model proxy

Run the pinned LiteLLM 1.94.2 CLI through `serve.py`. It retains upstream
configuration and Prisma migrations, and supplies an explicit IPv4/IPv6 socket.
`host=::` alone is IPv6-only on Linux. Railway sets `PORT` (default 4000).
Use one process/replica for this pilot; distributed concurrency needs shared Redis
counters before scaling this service.

Each model deployment has explicit USD/token rates, including cache reads and
both 5-minute and 1-hour cache writes. Rates were checked on 2026-11-15 against
[Anthropic's pricing](https://platform.claude.com/docs/en/about-claude/pricing).
Sonnet 5 is $2/$10 per million input/output tokens; cache read/5m write/1h write
are $0.20/$2.50/$4. Haiku 4.5 is $1/$5, with cache $0.10/$1.25/$2.
Recheck these when changing model pins. Alias lookup is not proof that accounting
works; tests calculate native Anthropic usage against every router deployment ID.

Control creates one non-resetting $10 sponsor team shared by that customer's
private and group keys. This is a **spending cutoff**, not a prepaid hard ceiling:
LiteLLM accounts after responses, so requests already running and delayed spend
updates can exceed $10. The proxy permits one concurrent request per key and four
globally, and clamps output to 8192 tokens. Existing smaller output limits remain.
Thinking budgets are clamped below output limits. This does not impose an exact
dollar overshoot bound: input size and delayed writes also affect spend.

Tests use the real pinned Router, native Anthropic cost handler, spend callback,
and team budget check, with a fake database writer/counter. They verify both DM
and group charges target one sponsor team, and that over-budget teams are rejected.
`scripts/smoke.py` additionally boots the real CLI, migrates a disposable PostgreSQL
schema, creates a team and two virtual keys through the admin APIs, and sends
native `/v1/messages` requests through a local synthetic provider. Verified
2026-11-15: three requests each accrued $3.84 including caches; shared team spend
reached $11.52, the next group request was denied with 429 before the provider,
output was capped at 8192, and spend-log message/response columns held no content.
No paid provider calls were made. Keep admission off until the same deployment
configuration passes its health and provider-specific first-contact gates.

```sh
pip install 'litellm[proxy]==1.94.2' pytest pyyaml
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest apps/trial-proxy/tests -q
```

Real PostgreSQL smoke (local database only; creates a unique schema):

```sh
pip install prisma psycopg[binary]
prisma generate --schema <site-packages>/litellm/proxy/schema.prisma
PROXY_SMOKE_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/test \
  python apps/trial-proxy/scripts/smoke.py
```

The inflated synthetic usage intentionally crosses the cutoff without purchasing
model tokens. Artifacts contain only synthetic data, under `/tmp/squire-proxy-smoke`.
