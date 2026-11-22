"""Shared scoped resource findings + in-flight probe dedupe (acceptance item 6)."""

from __future__ import annotations

import json

import pytest

from squire_overlay.guardrails import findings as F


class Clock:
    t = 5000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


def test_findings_scoped_to_origin_task_and_visible_to_siblings(hermes_env, clock):
    st = F.Findings(scope="agent:whatsapp:dm:111", now=clock)
    st.record(host="shop.example.com", kind="auth_required", detail="redirected to /signin", task_id="samsung")
    got = F.Findings(scope="agent:whatsapp:dm:111", now=clock).lookup("shop.example.com")
    assert got and got[0]["kind"] == "auth_required" and got[0]["task_id"] == "samsung"
    # another user's task sees nothing
    assert F.Findings(scope="agent:whatsapp:dm:222", now=clock).lookup("shop.example.com") == []


def test_only_safe_kinds_are_accepted(hermes_env, clock):
    st = F.Findings(scope="s", now=clock)
    assert st.record(host="x", kind="auth_required", detail="", task_id="t")
    assert st.record(host="x", kind="site_unreachable", detail="", task_id="t")
    assert st.record(host="x", kind="readonly_evidence", detail="price page loaded", task_id="t", handle="/ws/x.json")
    assert not st.record(host="x", kind="promo_code", detail="SAVE20", task_id="t")       # purchase-specific: refused
    assert not st.record(host="x", kind="cookie", detail="sid=abc", task_id="t")          # private: refused
    assert [f["kind"] for f in st.lookup("x")] == ["auth_required", "site_unreachable", "readonly_evidence"]


def test_findings_expire(hermes_env, clock):
    st = F.Findings(scope="s", now=clock, ttl_s=600)
    st.record(host="x", kind="site_unreachable", detail="", task_id="t")
    clock.t += 601
    assert st.lookup("x") == []


def test_classify_result_into_finding():
    assert F.classify_result("https://shop.example.com/p/1", '{"success": true, "output": "redirected to https://shop.example.com/signin?next=..."}') == "auth_required"
    assert F.classify_result("https://shop.example.com/p/1", '{"success": false, "output": "net::ERR_NAME_NOT_RESOLVED"}') == "site_unreachable"
    assert F.classify_result("https://shop.example.com/p/1", '{"success": true, "output": "Price $999"}') is None


# What browser-use's page_info() prints (dict repr), followed by get_text()-style page text.
AMAZON_PRODUCT_PAGE = (
    "{'url': 'https://www.amazon.com/dp/B0ABC123', 'title': 'Amazon.com: Widget 3000', 'w': 1280, 'h': 720}\n"
    "Skip to main content\n"
    "Hello, sign in\n"
    "Account & Lists\n"
    "<a href=\"https://www.amazon.com/ap/signin?openid.return_to=https%3A%2F%2Fwww.amazon.com%2F\">Sign in</a>\n"
    "Returns & Orders  Cart\n"
    "Widget 3000 — $999.00\n"
    "Enter the characters you see below (captcha) only if prompted.\n"
    "Terms: https://www.amazon.com/gp/help/customer/display.html?nodeId=login-help\n"
)
AMAZON_SIGNIN_PAGE = (
    "{'url': 'https://www.amazon.com/ap/signin?openid.pape.max_auth_age=0&openid.return_to=...', "
    "'title': 'Amazon Sign-In', 'w': 1280, 'h': 720}\nEmail or mobile phone number\n"
)


def test_sign_in_links_and_menu_text_on_a_normal_page_are_not_auth_required():
    """The amazon-style false positive: a product page whose header carries 'Hello, sign in', an
    /ap/signin href and the word captcha in body text must NOT mark the host auth_required (that
    would block every sibling's calls to amazon.com for 30 min)."""
    out = json.dumps({"success": True, "output": AMAZON_PRODUCT_PAGE})
    assert F.classify_result("https://www.amazon.com/dp/B0ABC123", out) is None
    assert F.classify_result("amazon.com", out) is None


def test_actual_redirect_to_the_sign_in_page_is_auth_required():
    out = json.dumps({"success": True, "output": AMAZON_SIGNIN_PAGE})
    assert F.classify_result("https://www.amazon.com/dp/B0ABC123", out) == "auth_required"
    # title alone (location missing) still counts
    out2 = json.dumps({"success": True, "output": "{'title': 'Robot Check', 'w': 1}\nType the characters you see"})
    assert F.classify_result("amazon.com", out2) == "auth_required"
    # an explicit error field counts; the same words in page text do not
    assert F.classify_result("x.example", json.dumps({"success": False, "error": "login required"})) == "auth_required"
    assert F.classify_result("x.example", json.dumps({"success": True, "output": "footer: login required for members area"})) is None


def test_auth_location_on_another_host_does_not_mark_this_host():
    out = json.dumps({"success": True, "output": "{'url': 'https://accounts.google.com/signin/v2', 'title': 'Google'}"})
    assert F.classify_result("https://shop.example.com/p/1", out) is None
    assert F.classify_result("accounts.google.com", out) == "auth_required"
    # subdomain of the same site counts as the same site
    out2 = json.dumps({"success": True, "output": "redirected to https://login.shop.example.com/sso"})
    assert F.classify_result("https://shop.example.com/p/1", out2) == "auth_required"


def test_no_code_is_ever_deduped():
    assert F.probe_key("browser_exec", {"code": "new_tab('https://a.example/x'); print(page_info())"}) is None
    assert F.probe_key("browser_exec", {"code": "js(\"document.querySelector('#buy').click()\")"}) is None
    assert F.probe_key("browser_exec", {"code": "fill_input('#q', 'x')"}) is None
    assert F.probe_key("terminal", {"command": "ls"}) is None
