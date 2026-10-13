from datetime import date

import pytest
from sqlalchemy import select

from enpra import approvals
from enpra.jobs import recruitment
from enpra.models import ApprovalRequest, Recruitment

from .conftest import RECRUIT_ROOM, at

TARGET = date(2026, 10, 14)


def test_prepare_notifies_with_links_and_does_not_send(db, ctx):
    at("2026-10-13 18:30")
    req = recruitment.prepare(db, ctx)
    assert req.status == "pending" and req.mode == "A"
    assert ctx.cw.posts == []
    kind, title, body = ctx.notifier.sent[-1]
    assert "承認依頼" in title
    for needle in ("業務内容", "対象者", "予定実行日時", "2026-10-13 19:00", "10月14日(水)", "/a/", "action=approve", "action=edit"):
        assert needle in body
    # 二重募集防止：同じ対象日の準備は同じ依頼を返す
    assert recruitment.prepare(db, ctx).id == req.id


def test_unapproved_at_1900_is_not_sent_and_renotified(db, ctx):
    at("2026-10-13 18:30")
    req = recruitment.prepare(db, ctx)
    at("2026-10-13 19:00")
    res = recruitment.send(db, ctx)
    assert res["status"] == "not_approved"
    assert ctx.cw.posts == []
    assert "再通知" in ctx.notifier.sent[-1][1]
    # 19:00 以降に承認 → 即時送信（一度だけ）
    at("2026-10-13 19:20")
    approvals.decide(db, ctx, req, "approve", actor="admin")
    assert req.status == "executed"
    assert len(ctx.cw.posts) == 1 and ctx.cw.posts[0][0] == RECRUIT_ROOM
    recruitment.send(db, ctx)
    approvals.run_due(db, ctx)
    assert len(ctx.cw.posts) == 1


def test_approved_before_1900_waits_then_sends_once(db, ctx):
    at("2026-10-13 18:35")
    req = recruitment.prepare(db, ctx)
    approvals.decide(db, ctx, req, "approve", actor="admin")
    assert req.status == "approved" and ctx.cw.posts == []
    at("2026-10-13 18:59")
    approvals.run_due(db, ctx)
    assert ctx.cw.posts == []
    at("2026-10-13 19:00")
    recruitment.send(db, ctx)
    recruitment.send(db, ctx)
    approvals.run_due(db, ctx)
    assert len(ctx.cw.posts) == 1
    rec = db.scalar(select(Recruitment).where(Recruitment.target_date == TARGET))
    assert rec.message_id and rec.posted_at


def test_edit_and_approve_uses_edited_text(db, ctx):
    at("2026-10-13 19:05")
    req = recruitment.prepare(db, ctx)
    approvals.decide(db, ctx, req, "edit_approve", actor="admin", edited_message="修正版の募集文")
    assert ctx.cw.posts[-1][1] == "修正版の募集文"


def test_skip_and_hold(db, ctx):
    at("2026-10-13 18:30")
    req = recruitment.prepare(db, ctx)
    approvals.decide(db, ctx, req, "hold", actor="admin")
    assert req.status == "held"
    approvals.decide(db, ctx, req, "skip", actor="admin")
    assert req.status == "skipped"
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(db, ctx, req, "approve", actor="admin")
    at("2026-10-13 19:00")
    recruitment.send(db, ctx)
    assert ctx.cw.posts == []


def test_timeout_never_auto_approves(db, ctx):
    at("2026-10-13 18:30")
    req = recruitment.prepare(db, ctx)
    at("2026-10-15 09:00")
    approvals.expire_stale(db, ctx)
    approvals.run_due(db, ctx)
    assert req.status == "expired"
    assert ctx.cw.posts == []


def test_system_cannot_approve_or_change_mode(db, ctx):
    at("2026-10-13 18:30")
    req = recruitment.prepare(db, ctx)
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(db, ctx, req, "approve", actor="system")
    with pytest.raises(approvals.ApprovalError):
        approvals.change_mode(db, "recruit_post", "B", "system")
    with pytest.raises(approvals.ApprovalError):
        approvals.change_mode(db, "qa_answer", "C", "admin")


def test_mode_b_posts_at_1900_without_approval_and_notifies(db, ctx):
    approvals.change_mode(db, "recruit_post", "B", "admin")
    at("2026-10-13 18:30")
    req = recruitment.prepare(db, ctx)
    assert req.status == "approved" and ctx.cw.posts == []
    at("2026-10-13 19:00")
    recruitment.send(db, ctx)
    assert len(ctx.cw.posts) == 1
    assert "実行完了" in ctx.notifier.sent[-1][1]


def test_automate_action_changes_mode_explicitly(db, ctx):
    at("2026-10-13 19:10")
    req = recruitment.prepare(db, ctx)
    approvals.decide(db, ctx, req, "automate", actor="admin", automate_mode="B")
    assert approvals.get_mode(db, "recruit_post") == "B"
    assert req.status == "executed"


def test_execute_is_exactly_once(db, ctx):
    at("2026-10-13 19:10")
    req = recruitment.prepare(db, ctx)
    approvals.decide(db, ctx, req, "approve", actor="admin")
    req.status = "approved"  # 何らかの理由で再実行されても
    approvals.execute(db, ctx, req)
    assert len(ctx.cw.posts) == 1  # Recruitment / idempotency key で二重投稿しない


def test_failure_is_recorded_and_retry_works(db, ctx):
    ctx.cw.fail_post_to.add(RECRUIT_ROOM)
    at("2026-10-13 19:10")
    req = recruitment.prepare(db, ctx)
    approvals.decide(db, ctx, req, "approve", actor="admin")
    assert req.status == "failed"
    assert "実行失敗" in ctx.notifier.sent[-1][1]
    ctx.cw.fail_post_to.clear()
    approvals.retry_failed(db, ctx, req, "admin")
    assert req.status == "executed" and len(ctx.cw.posts) == 1
    assert db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == req.id)).attempts == 2
