import base64
import hashlib
import hmac
import re

import httpx
from fastapi.testclient import TestClient

from enpra.chatwork import ChatworkClient
from enpra.security import hash_password, verify_chatwork_signature, verify_password

from .conftest import RECRUIT_ROOM, at


def _app(settings, ctx):
    from enpra.web import create_app

    settings.admin_password_hash = hash_password("correct horse battery", iterations=1000)
    return TestClient(create_app(settings, ctx), base_url="https://testserver", follow_redirects=False)


def _login(c):
    r = c.post("/login", data={"password": "correct horse battery", "next": "/"})
    assert r.status_code == 303


def _token_from_notice(ctx):
    body = ctx.notifier.sent[-1][2]
    return re.search(r"/a/([A-Za-z0-9_\-]+)\?action=approve", body).group(1)


def test_approval_link_requires_login_and_is_single_use(settings, ctx):
    c = _app(settings, ctx)
    at("2026-10-13 19:05")
    c.post("/jobs/recruit-prepare", headers={"X-Job-Secret": "jobsecret"})
    token = _token_from_notice(ctx)

    r = c.get(f"/a/{token}?action=approve")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert ctx.cw.posts == []

    _login(c)
    page = c.get(f"/a/{token}")
    assert page.status_code == 200 and "承認して即時送信" in page.text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)

    # CSRF なしは拒否
    bad = c.post("/approvals/1/decide", data={"action": "approve", "token": token})
    assert bad.status_code == 400 and ctx.cw.posts == []

    ok = c.post("/approvals/1/decide", data={"action": "approve", "token": token, "csrf": csrf,
                                             "message": "x", "selected": "group"})
    assert ok.status_code == 303
    assert len(ctx.cw.posts) == 1 and ctx.cw.posts[0][0] == RECRUIT_ROOM

    again = c.get(f"/a/{token}")
    assert "無効" in again.text
    c.post("/approvals/1/decide", data={"action": "approve", "csrf": csrf})
    assert len(ctx.cw.posts) == 1


def test_job_endpoint_requires_secret(settings, ctx):
    c = _app(settings, ctx)
    assert c.post("/jobs/recruit-prepare").status_code == 401
    assert c.post("/jobs/recruit-prepare", headers={"X-Job-Secret": "nope"}).status_code == 401
    assert c.post("/jobs/unknown", headers={"X-Job-Secret": "jobsecret"}).status_code == 404


def test_locked_mode_cannot_change_via_ui(settings, ctx):
    c = _app(settings, ctx)
    _login(c)
    page = c.get("/modes")
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    r = c.post("/modes/qa_answer", data={"mode": "C", "csrf": csrf})
    assert "変更できません" in r.text
    r = c.post("/modes/daily_report", data={"mode": "C", "csrf": csrf})
    assert r.status_code == 303


def test_password_and_signature():
    h = hash_password("pw-123456789", iterations=1000)
    assert verify_password("pw-123456789", h) and not verify_password("x", h)
    token = base64.b64encode(b"secret-key").decode()
    body = b'{"webhook_event_type":"message_created"}'
    sig = base64.b64encode(hmac.new(b"secret-key", body, hashlib.sha256).digest()).decode()
    assert verify_chatwork_signature(token, body, sig)
    assert not verify_chatwork_signature(token, body + b"x", sig)


def test_chatwork_client_dry_run_never_writes_and_merges_members():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.content.decode()))
        if req.method == "GET" and req.url.path.endswith("/members"):
            return httpx.Response(200, json=[{"account_id": 1, "role": "admin"}, {"account_id": 2, "role": "member"}])
        return httpx.Response(200, json={"ok": True})

    dry = ChatworkClient("t", dry_run=True, transport=httpx.MockTransport(handler))
    assert dry.post_message(1, "hi")["dry_run"]
    dry.add_room_members(10, [3])
    assert [c[0] for c in calls] == ["GET"]  # 読み取りのみ

    calls.clear()
    live = ChatworkClient("t", dry_run=False, transport=httpx.MockTransport(handler))
    live.add_room_members(10, [3, 2])
    put = [c for c in calls if c[0] == "PUT"][0]
    assert "members_admin_ids=1" in put[2] and "members_member_ids=2%2C3" in put[2]


def test_chatwork_client_retries_429():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(429) if n["i"] < 3 else httpx.Response(200, json={"account_id": 1})

    c = ChatworkClient("t", dry_run=True, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.me() == {"account_id": 1} and n["i"] == 3
