# WhatsApp integration — research and recommendation

Written 2026-09-28 from research done 2026-09-27 (three research passes plus a focused look at
Instinct; primary sources re-fetched that day). Status: **parked**. Pick up after overlay Phase ③
and the Phase ④ base bump, which are prerequisites for any build.

**Founder decision (2026-09-27):** build the **official WhatsApp Cloud API** path once. It is
compliant for European (EEA) numbers from day one. US (+1) numbers go behind a separate switch that
is flipped only after a US counsel read, with the risk controls in §6.

---

## 1. The policy constraint (the thing that shapes everything)

- **Meta Terms for WhatsApp Business Platform §4.7** (last modified 2026-09-23) say "AI Providers",
  explicitly including "general-purpose artificial intelligence assistants", are "strictly
  prohibited from accessing or using the WhatsApp Business Platform, whether directly or
  indirectly", when AI is "the primary (rather than incidental or ancillary) functionality", "as
  determined by Meta in its sole discretion". Squire clearly qualifies; we found no honest framing
  that avoids it. The "retain an AI Provider as your Solution Provider" carve-out is for non-AI
  businesses that hire an AI vendor.
- **The only exception is by the recipient's phone country**, in markets "where Meta is legally
  required to permit this use case" (Meta's AI-provider pricing page):
  - **EEA (30 countries): allowed and free**, because the European Commission imposed interim
    measures on 2026-06-09 (case AT.41034). ChatGPT returned to WhatsApp for EEA numbers on
    2026-07-13. Meta is appealing, so this can be withdrawn.
  - **Brazil (+55): allowed but paid**, about $0.0625 per message since 2026-03-11. Brazil's
    regulator (CADE) is disputing the fee in court.
  - **US, UK (+44 is not EEA), India, Turkey: prohibited at any price.**
- **The unofficial route breaks the consumer terms.** WhatsApp's ToS bans automated access,
  "non-personal use" and unofficial clients, and the Messaging Guidelines (2026-09-23) ban
  "unofficial clients … or automation". Bans land on whichever number runs the client.
- **Squire's PRD already rules out Baileys/unofficial WhatsApp** for v1 (`docs/prd.md:10, :27`).

### Pricing change from 2026-10-01

Meta now bills **every** service (free-form) message a business sends, at the recipient market's
utility rate: US ≈ $0.0034, UK ≈ $0.022, Germany ≈ $0.055, Spain ≈ $0.020. Inbound stays free.
It is unresolved whether the EC's "free of charge" order exempts EEA AI providers. This swings a
chatty German user from ~$0 to ~$22/month.

Outside the 24-hour customer-service window only pre-approved **templates** can be sent. Marketing
templates to US numbers have been blocked since 2025-04-01 (error 131049), so proactive messages to
US users must be genuine **utility** templates or they stop being delivered.

## 2. How others do it

- **Instinct** (Spear Street Technology, $350M Series B, Aug 2026):
  - Its "act on your WhatsApp" connector linked Squire-style to the **user's own account** through
    WhatsApp's unofficial Linked Devices protocol (QR / pairing code, per archived copies of its
    web-app code). It was **removed between 2026-08-31 and 2026-09-02**.
  - The chat with Instinct runs on a WhatsApp number Instinct operates (`whatsAppChannelPhoneNumber`
    per user). Whether that is the official API or an ordinary account is not public. Either way a
    US general-purpose agent is outside Meta's rules.
  - Its terms put the risk on the user ("You are solely responsible…").
- **Poke** (closest comparable; official API) complied: WhatsApp off except Italy/Brazil, moved US
  users to Telegram (Feb 2026) and iMessage/SMS, became the first AI agent approved on Apple
  Messages for Business (Jun 2026), back on WhatsApp in the EEA after the EC order. Its maker was
  the complainant in the EC case.
- **Catch** ($99/mo, launched ~2026-09-02) says it "operates through WhatsApp Business with its own
  dedicated Catch phone number" in the US. **Arlo** is reply-only on WhatsApp and sends proactive
  messages via iMessage/SMS. Neither has been reported cut off. That is evidence such setups can
  run for weeks, not that Meta tolerates them.
- **ChatGPT, Copilot, Perplexity, Zapia, Luzia** left WhatsApp on 2026-01-15 after Meta's notices.
- **Meta launched its own personal agent, Muse, inside WhatsApp for US users on 2026-09-08.**

## 3. How Meta enforces (matters for the US decision)

- There is **no AI self-declaration** at onboarding and no AI-specific API error. The January
  cut-off was enforced by **notices** to known providers (reported 90-day grace), not API blocks.
- Billing is tagged by sender ("delivered from an AI Provider"; webhook `pricing.category =
  general_purpose_ai`, analytics `AI_BOT`), so Meta holds a per-business flag it assigns itself.
  **Inference:** an unflagged business sending to +1 is delivered and billed normally until Meta
  flags it; then it faces a discretionary integrity action.
- Relevant error codes if that happens: 368 / 131031 (account restricted or disabled for policy),
  130497 (restricted from messaging certain countries; can arrive later on the status webhook).
- **Blast radius:** §5.3 allows immediate suspension if "you or your Clients" breach, §5.2
  termination for any reason, and the Business Messaging Policy lets Meta "prohibit you and your
  organization from all future use of WhatsApp products". That can take down a compliant EEA
  channel on the same business portfolio (and possibly Meta ad accounts).
- A second, independent requirement: automation must offer "prompt, clear, and direct escalation
  paths" (a human route), whatever §4.7 says.

## 4. Options for US users (ranked)

| # | Route | Terms | If it goes wrong | Verdict |
|---|---|---|---|---|
| 1 | **Official Cloud API with +1 allowed** (same build as EEA, +1 added to the country gate) | Breaks Meta's business terms (contract only; no computer-access or wiretap angle while inside the API) | Squire's business account suspended; users' personal numbers untouched; users fail over to Telegram | **Least-bad gray route — chosen, behind a switch** |
| 2 | Squire-owned numbers on an open-source multi-device client (whatsmeow) in a central gateway | Breaks WhatsApp consumer ToS; replacing banned numbers is a further breach | Numbers restricted within hours (a 100-user inbound-only bot was restricted in ~2 h, Baileys #1850); since July 2026 companions can't message never-contacted numbers; each number needs a real phone used every 14 days (phone farm) | Not recommended |
| 3 | Link Squire as a device on the user's own WhatsApp | Consumer ToS, plus wiretap exposure from the user's contacts (e.g. CIPA §631/632, $5,000 per violation) | The **user's** personal account is banned | Hard no — Instinct built it and removed it |
| 4 | "Doorbell": link-only utility pings, chat happens elsewhere | Least gray, not a safe harbor | Pings reclassified as marketing stop arriving in the US | Adjunct only, not a chat channel |
| 5 | Meta platforms: Muse Connectors, Meta Business Agent, "human concierge" framing | — | Muse makes Squire a tool inside Meta's rival agent; "human concierge" is honest only if humans really answer every thread | Not a delivery route. Business Agent is fine for Squire's own sales/support bot |
| 6 | Compliant only: WhatsApp for EEA; Telegram now and iMessage/SMS/Apple Messages for Business for the US | Compliant | Contained | The sound baseline |

**Legal bright line (for counsel to confirm):** continuing after any Meta notice, ban or
cease-and-desist — or re-onboarding under a new account/portfolio/entity — is what turned
terms-of-service problems into computer-access liability, with the founder personally liable, in
*Facebook v. Power Ventures* (9th Cir. 2016). Onboarding a new account after a ban also breaches
WhatsApp Terms §3 and Platform Terms 7.e (circumvention).

**Audience check:** the PRD and ad plan target US tech-adjacent users (Reddit + newsletters). Only
~32% of US adults use WhatsApp (Pew, 2025), and iPhones were ~59% of Q1-2026 US sales, so
iMessage/SMS is the US-native equivalent. The waitlist has no country field, so the belief that
"most users will use WhatsApp" can't be tested yet.

## 5. Architecture (official Cloud API, shared number)

Squire already has the central router this needs: `apps/ingress` (today `POST /telegram/{bot_id}`
→ buffer-and-wake → tenant shim; private-network wake proven 2026-08-13, ~14 s cold, typing
indicator in < 0.5 s).

- **Number:** one Squire-owned business number on a dedicated, verified business portfolio (EEA
  and US traffic on **separate portfolios**). A number per tenant doesn't scale: a portfolio is
  capped at 2 numbers, 20 after verification; BSUIDs are portfolio-scoped; display names must match
  the business.
- **Inbound:** Meta posts all events to one app webhook → new `GET/POST /whatsapp` in ingress:
  verify-token handshake; verify `X-Hub-Signature-256` over the raw body; return 200 fast (Meta
  retries non-200s for up to 7 days); dedupe by message ID; split by sender; map BSUID/wa_id →
  tenant via a new control-db table (keyed by an HMAC; needs a `test_privacy_schema.py` whitelist
  edit); **fail-closed country gate** (BSUID ISO prefix / calling code in the allowed set); re-sign
  per-tenant and forward to a new shim route. Buffer-and-wake carries over.
- **Outbound — the egress service (workstream 1H, `docs/implementation-plan.md:250-254`):** the
  shared system-user token can message **every** Squire user, and anything in a tenant container is
  readable by that tenant's agent, so the token can't live in tenants. Egress holds it, pins the
  recipient to the tenant's bound user, re-checks the country gate on every send, proxies media,
  meters, tracks the 24 h window, sends utility templates outside it, and consumes failed-status
  webhooks. Built once, it also removes the Telegram bot-pool ceiling (5/5 on staging).
- **Tenant image:** reuse upstream Hermes' Cloud adapter (`gateway/platforms/whatsapp_cloud.py`) —
  it has no background loops, so tenants still sleep. Gaps to cover:
  - `GRAPH_API_BASE` is hard-coded → small patch/override to point it at egress.
  - **No template or 24 h-window support** at 8.3, 9.24 or upstream main.
  - Out-of-window failures (131047) can arrive only as a status webhook the adapter just debug-logs
    (upstream issue #80056), so cron would record undelivered briefings as delivered.
  - Default Graph API version v20.0 **expired 2026-09-24** → pin `WHATSAPP_CLOUD_API_VERSION`.
  - 8.3 turns reactions/system events into blank (billable) agent turns; 9.24 fixes this and
    rewrote the adapter (2111 → 1021 lines). **Build after the Phase ④ base bump.**
  - `WHATSAPP_CLOUD_*` isn't stripped from the agent's environment — another reason the real token
    must stay in egress.
  - Per-platform: a `whatsapp_cloud` config block (streaming and tool-progress off — every message
    is billed), platform-aware health probes and concierge copy, a whatsapp target for the
    "you're connected" message, and `gateway_restart_notification: false` pinned for the WhatsApp
    platform too (the Phase ③ managed key covers Telegram only).
- **Onboarding:** signup asks WhatsApp or Telegram → `wa.me/<number>?text=<one-time code>` link (QR
  on desktop) → user taps send (opens the window) → ingress binds the user to the tenant with a
  single-use nonce **with a TTL** (today's `bind_nonce` has none) → non-allowed countries are sent
  to Telegram.
- **Don't use** the upstream Baileys bridge: QR-only, defaults to self-chat on the user's own
  number, its 30 s keepalive keeps tenants awake (~$9.4–11.9/month RAM vs ~$0.5–4 sleeping), and
  it's the PRD's non-goal.

**Estimated effort:** ~5–7 engineer-weeks after Phases ③/④ (ingress ~1 wk; control-db mapping +
binding ~3–4 days; egress ~2 wk; tenant image ~1 wk; signup ~2–3 days; end-to-end + sleep/wake
drill ~1 wk), plus 1–4 calendar weeks of Meta business verification, display-name and template
approval in parallel.

**Per-user Meta cost (estimates, 2026-10-01 rates):** US ~$0.15/mo proactive-only (~43 messages)
to ~$1.37/mo for a chatty user (~400 outbound); EEA depends on the unresolved exemption (Germany
~$2.37 to ~$22/mo if billed). A flat $10 Pro tier needs a message allowance or regional pricing if
EEA messages are billed. Brazil at $0.0625/msg (~$25/mo per chatty user) stays out.

## 6. Controls for the US switch (must be in place before +1 is enabled)

1. **Stop permanently on any Meta notice, restriction (368/131031/130497), lock or C&D.** Never
   re-onboard a new account, portfolio, number or entity after a ban.
2. **Kill switch:** one flip disables WhatsApp send/receive and moves every affected user to
   Telegram with a pre-written message. WhatsApp must never be anyone's only channel.
3. **No detection or ban evasion** as policy (hiding hosting, device spoofing, re-creating
   accounts).
4. **Everything said to Meta or a BSP is true** — business description, vertical, display name,
   template categories, app-review use case, any appeal. Never frame Squire as "ancillary AI" or a
   "work tool".
5. **Human escalation path** in the WhatsApp experience (required by the Business Messaging Policy).
6. **Separate business portfolio** for EEA vs US traffic (reduces, doesn't eliminate, spillover).
7. **Watch status webhooks, not just API responses:** alert on the first `general_purpose_ai` /
   `AI_BOT` tag on Squire's traffic (= Meta has classified Squire) and on async 130497/131047.
8. **Written US counsel read before onboarding**, plus a plan for investor disclosure.
9. **Mind public copy:** Meta Terms §7.4 require mutual written approval for publicity, and
   WhatsApp says it acts on off-platform marketing claims.

## 7. Open questions and cheap tests (before building)

- EU counsel: does the EC interim order protect a small non-EU startup serving EEA numbers, and is a
  phone-country gate enough? (Plus EU VAT from first sale and GDPR.)
- US counsel: the questions in §4's bright line; dispute venue (WhatsApp Terms vs Meta Commercial
  Terms arbitration); money exposure beyond termination; whether a ban could reach Meta ad accounts.
- Sandbox WABA tests: EEA and US test numbers; Oct-1 billing and `pricing.category`; template
  categorisation (does "your briefing is ready" pass as UTILITY?); whether 131047 comes back
  synchronously or only on the status webhook; how the BSUID country prefix behaves.
- Ask Meta Direct Support / a BSP honestly how a business becomes an "AI Provider" (asking creates
  no misrepresentation).
- **Add a phone-country field to the waitlist** so WhatsApp demand can be split by market.

## 8. Things to fix regardless (not done yet)

- **The live site over-promises.** thesquire.co (origin/main `1ada0a7`, PR #5, 2026-08-29) says
  "WhatsApp and Telegram, from day one" and "Your assistant messages you first"; `llms.txt` says
  "WhatsApp and Telegram, both at launch"; the JSON-LD lists `operatingSystem: "Telegram,
  WhatsApp"`. Neither claim can be delivered at launch, and "messages you first" isn't literally
  true on Telegram either (the user taps Start). The local checkout was behind origin/main — pull
  before editing.
- The in-tenant concierge facts (`home-template/skills/concierge/state-machine.yaml:92-98`) and the
  PRD (`prd.md:10, 27, 128`, `implementation-plan.md:259`) still describe WhatsApp as a Pro-tier
  add-on with a per-tenant number at US prices — inconsistent with the site and unworkable (number
  caps). Update when the WhatsApp design is written.

## Key sources

- Meta Terms for WhatsApp Business Platform (mod. 2026-09-23): https://www.facebook.com/legal/Meta-Terms-for-WhatsApp-Business-Platform
- Meta AI-provider pricing page: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/ai-providers
- Meta non-template pricing (Oct 1 change): https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages
- WhatsApp error codes: https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes
- WhatsApp Business Messaging Policy (2026-09-23): https://whatsappbusiness.com/policy/
- European Commission IP/26/1276 (2026-06-09): https://ec.europa.eu/commission/presscorner/detail/en/ip_26_1276
- TechCrunch, Brazil carve-out (2026-01-15): https://techcrunch.com/2026/01/15/after-italy-whatsapp-excludes-brazil-from-rival-chatbot-ban
- Poke release notes: https://poke.com/docs/release-notes
- Catch on WhatsApp: https://www.catchagent.ai/integrations/whatsapp
- Arlo on WhatsApp: https://www.arlo.sh/whatsapp
- Meta Muse launch (2026-09-08): https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/
- Archived Instinct app code, Aug 31 / Sep 2: https://web.archive.org/web/20260831130952/https://app.instinct.co/assets/app-CDUdceg2.js , https://web.archive.org/web/20260902145647/https://app.instinct.co/assets/app-DqvA9lIX.js
- Baileys #1850 (fast restriction): https://github.com/WhiskeySockets/Baileys/issues/1850
- whatsmeow #1197 (companions can't message first): https://github.com/tulir/whatsmeow/issues/1197
- Facebook v. Power Ventures (9th Cir. 2016): https://cdn.ca9.uscourts.gov/datastore/opinions/2016/12/09/13-17102.pdf

Related project memory: `whatsapp-meta-ai-policy.md`.
