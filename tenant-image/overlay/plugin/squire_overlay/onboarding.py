"""Pilot-only onboarding and product facts; legacy concierge stays unchanged."""
import os

POLICY = """Squire WhatsApp pilot facts: this pilot supports 20 initial customers on one shared WhatsApp number. A customer's trial starts on their first accepted model-backed request, lasts 72 hours, and allows 75 substantive turns per 24-hour trial day shared across their private agent and approved groups. The shared model spending cutoff is $10 for the whole trial; usage is accounted after responses, so in-flight work can exceed that cutoff slightly. It is not a prepaid balance or a promise of 75 turns regardless of spending. Imported founder access and its sponsored groups have no customer trial expiry; group funding still has a separate shared spending cutoff. The dashboard is authoritative for status; do not invent remaining usage. WhatsApp, supported attachments and approved groups are pilot features, not paid add-ons. There is no published paid pilot subscription or checkout: do not quote old Starter/Pro prices or promise automatic subscription. Trial expiry pauses service; an operator can review an extension. Users can pause/delete in the dashboard or send STOP. Explain limits only when relevant or asked. Never claim that connecting a provider removes trial access limits. Offer provider setup only if requested, using the existing connect tool; never ask anyone to paste a key into a group. Group private actions require separate owner approval, and sharing a result needs separate release consent."""


def context(**kwargs):
    founder=os.environ.get("SQUIRE_RESTORED_HOME")=="true"
    identity=(" This is the imported founder's private agent: preserve established identity and preferences; do not restart onboarding or describe the founder as a 72-hour trial customer. Founder private access has no trial expiry. Approved group model funding is separate from the founder's existing private provider credentials." if founder else " For a new private customer, help with their actual request immediately; ask their preferred name or timezone only when useful. Do not force provider connection or a setup questionnaire before useful work.")
    return {"context":POLICY+identity}


def prepare(home,atomic):
    # Replace only the pilot's image-owned concierge guidance. Founder's own
    # identity/SOUL and other explicitly promoted skills are left intact.
    atomic(home/"skills/concierge/SKILL.md",'''---
name: concierge
description: Squire WhatsApp pilot onboarding and accurate trial information.
---
Help with the current request immediately. Ask name/timezone only when useful.
Read state-machine.yaml for pilot facts. Do not use legacy Starter/Pro pricing.
Connect a model provider only when requested; never collect credentials in groups.
''')
    import yaml
    atomic(home/"skills/concierge/state-machine.yaml",yaml.safe_dump({"version":"whatsapp-pilot-v1","initial":"complete","completion_state":"complete","facts":{"pilot":POLICY}},sort_keys=False))
    atomic(home/".squire/concierge-state.json",'{"state":"complete"}\n')
