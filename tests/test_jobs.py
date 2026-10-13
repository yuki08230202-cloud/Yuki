from datetime import date

from sqlalchemy import select

from enpra import approvals
from enpra.jobs import contacts, daily_report, indeed, invoices, participation, qa, recruitment
from enpra.models import Applicant, DailyRecord, Invoice, TextTemplate, Worker
from enpra.templates_text import replace_chatwork_id

from .conftest import ADMIN, RECRUIT_ROOM, at, msg


def _post_recruit(db, ctx):
    at("2026-10-13 18:30")
    req = recruitment.prepare(db, ctx)
    at("2026-10-13 18:40")
    approvals.decide(db, ctx, req, "approve", actor="admin")
    at("2026-10-13 19:00")
    recruitment.send(db, ctx)
    return ctx.cw.posts[-1]


# ---------------------------------------------------------------- 参加表明・個別依頼
def test_participation_by_reply_with_dedupe_and_cancel(db, ctx):
    _post_recruit(db, ctx)
    rmid = "9001"
    rp = lambda aid: f"[rp aid={ADMIN} to={RECRUIT_ROOM}-{rmid}]渡邉勇貴さん\n"  # noqa: E731
    ctx.cw.room_messages[RECRUIT_ROOM] = [
        msg(1, 11, "佐藤", rp(11) + "参加します", "2026-10-13 19:05"),
        msg(2, 11, "佐藤", "参加", "2026-10-13 19:06"),                       # 重複
        msg(3, 12, "鈴木", rp(12) + "参加", "2026-10-13 19:10"),
        msg(4, 13, "高橋", "参加", "2026-10-13 19:20"),
        msg(5, 13, "高橋", "すみません、キャンセルします", "2026-10-13 19:30"),  # 取り消し
        msg(6, 14, "田中", "明日は参加できるか分かりません、また連絡します", "2026-10-13 19:40"),  # 曖昧 → 対象外
        msg(7, ADMIN, "渡邉", "参加", "2026-10-13 19:50"),                     # 管理者は除外
        msg(8, 15, "伊藤", "参加", "2026-10-13 18:00"),                         # 募集前 → 対象外
        msg(9, 16, "山本", "参加", "2026-10-13 23:00"),                         # 締切後 → 対象外
    ]
    at("2026-10-13 22:00")
    req = participation.prepare_requests(db, ctx)
    assert req.status == "pending"
    assert sorted(t["name"] for t in req.targets) == ["佐藤", "鈴木"]

    ctx.cw.contact_list = [{"account_id": 11, "room_id": 811}]
    approvals.decide(db, ctx, req, "approve", actor="admin", selected_keys=["11", "12"])
    rooms = [r for r, _ in ctx.cw.posts[1:]]
    assert rooms == [811, RECRUIT_ROOM]  # 鈴木さんはダイレクトなし → グループで宛先指定
    assert "佐藤さん" in ctx.cw.posts[1][1]
    assert "[To:12]" in ctx.cw.posts[2][1]


def test_individual_approval_subset(db, ctx):
    _post_recruit(db, ctx)
    ctx.cw.room_messages[RECRUIT_ROOM] = [
        msg(1, 11, "佐藤", "参加", "2026-10-13 19:05"),
        msg(2, 12, "鈴木", "参加", "2026-10-13 19:05"),
    ]
    at("2026-10-13 22:00")
    req = participation.prepare_requests(db, ctx)
    approvals.decide(db, ctx, req, "approve", actor="admin", selected_keys=["12"])
    assert len(ctx.cw.posts) == 2 and "鈴木さん" in ctx.cw.posts[-1][1]


# ---------------------------------------------------------------- Indeed
def test_indeed_flow_with_chatwork_id_replacement(db, ctx):
    db.add(TextTemplate(name="indeed_guide", body="{name}様\nご応募ありがとうございます。\nChatwork ID：old_id_123\nよろしくお願いします。"))
    csv_text = "external_id,name,job_title,applied_at,contact_url\n" + "\n".join(
        f"ind-{i},応募者{i},フォーム営業,2026-10-0{i},https://employers.indeed.com/x/{i}" for i in range(1, 5)
    )
    assert indeed.import_csv(db, csv_text) == {"added": 4, "updated": 0}
    req = indeed.prepare_guides(db, ctx)
    assert len(req.targets) == 4 and req.mode == "A"
    assert "i4snxm1oompx" in req.message and "old_id_123" not in req.message
    approvals.decide(db, ctx, req, "approve", actor="admin", selected_keys=["ind-1", "ind-2", "ind-3"])
    statuses = {a.external_id: a.status for a in db.scalars(select(Applicant))}
    assert statuses == {"ind-1": "approved_manual_send", "ind-2": "approved_manual_send",
                        "ind-3": "approved_manual_send", "ind-4": "new"}
    a1 = db.scalar(select(Applicant).where(Applicant.external_id == "ind-1"))
    indeed.mark_sent(db, a1.id, "admin")
    assert a1.status == "guided"
    # 次回は未対応の ind-4 のみ
    nxt = indeed.prepare_guides(db, ctx)
    assert [t["key"] for t in nxt.targets] == ["ind-4"]


def test_replace_chatwork_id_variants():
    for s in ("Chatwork ID：abc", "ChatworkID:abc", "チャットワークID： abc"):
        out, n = replace_chatwork_id(s, "i4snxm1oompx")
        assert n == 1 and out.endswith("i4snxm1oompx")


# ---------------------------------------------------------------- コンタクト上限
def test_contacts_warning_and_limit(db, ctx):
    ctx.cw.contact_list = [{"account_id": i, "room_id": i} for i in range(15)]
    ctx.cw.requests = [{"request_id": 1, "account_id": 99, "name": "新人", "chatwork_id": "new", "message": "Indeed応募"}]
    res = contacts.check(db, ctx)
    assert res["remaining"] == 5 and len(res["approvals"]) == 1
    assert any("上限に近づいて" in t for _, t, _ in ctx.notifier.sent)

    # 上限到達後は承認済みでも実行時に停止
    ctx.cw.contact_list = [{"account_id": i, "room_id": i} for i in range(20)]
    req = approvals.ApprovalRequest  # noqa: F841
    from enpra.models import ApprovalRequest

    r = db.get(ApprovalRequest, res["approvals"][0])
    approvals.decide(db, ctx, r, "approve", actor="admin")
    assert r.status == "failed" and ctx.cw.approved == []
    ctx.cw.requests.append({"request_id": 2, "account_id": 98, "name": "新人2"})
    res2 = contacts.check(db, ctx)
    assert res2["approvals"] == []
    assert any("上限到達" in t for _, t, _ in ctx.notifier.sent)


# ---------------------------------------------------------------- 質問回答
def test_qa_faq_grounded_vs_sensitive(db, ctx):
    qa.add_faq(db, "フォームが送信できない時はどうすればいいですか", "送信不可として記録し、次の企業へ進んでください。", "faq")
    m1 = msg(1, 11, "佐藤", "フォームが送信できない時はどうすればいいですか？", "2026-10-13 10:00")
    r1 = qa.handle_message(db, ctx, RECRUIT_ROOM, m1)
    assert r1.job_type == "qa_answer_faq" and "送信不可" in r1.message

    m2 = msg(2, 12, "鈴木", "報酬の単価はいくらですか？", "2026-10-13 10:05")
    r2 = qa.handle_message(db, ctx, RECRUIT_ROOM, m2)
    assert r2.job_type == "qa_answer" and r2.mode == "A"
    assert any("報酬" in w for w in r2.warnings)

    # FAQ 回答を MODE B にしても、報酬の質問は確認必須のまま
    approvals.change_mode(db, "qa_answer_faq", "B", "admin")
    m3 = msg(3, 13, "高橋", "報酬の振込日はいつですか？", "2026-10-13 10:10")
    assert qa.handle_message(db, ctx, RECRUIT_ROOM, m3).status == "pending"

    # 承認済み回答は次回優先利用される
    approvals.decide(db, ctx, r2, "edit_approve", actor="admin", edited_message="単価は契約書をご確認ください。")
    m4 = msg(4, 14, "田中", "報酬の単価はいくらですか？", "2026-10-13 11:00")
    r4 = qa.handle_message(db, ctx, RECRUIT_ROOM, m4)
    assert r4.message == "単価は契約書をご確認ください。" and r4.status == "pending"


def test_qa_ignores_non_questions(db, ctx):
    assert qa.handle_message(db, ctx, RECRUIT_ROOM, msg(1, 11, "佐藤", "参加", "2026-10-13 10:00")) is None
    assert qa.handle_message(db, ctx, RECRUIT_ROOM, msg(2, ADMIN, "渡邉", "質問ある？", "2026-10-13 10:00")) is None


# ---------------------------------------------------------------- 日報
def test_parse_report():
    assert daily_report.parse_report("終了報告\n送信完了：45件\n送信不可：5件\nアポ：1") == {
        "sent": 45, "unsendable": 5, "appointments": 1}
    assert daily_report.parse_report("お疲れ様です") is None


class FakeSheets:
    dry_run = True

    def __init__(self, worklog):
        self.worklog = worklog
        self.upserts = []

    def worklog_counts(self, d):
        return self.worklog

    def upsert_daily_records(self, db, d, records, aid):
        self.upserts.append((d, records))
        return []

    def available_list_count(self):
        return None


def test_daily_report_mode_b_and_mismatch(db, ctx):
    ctx.cw.room_messages[RECRUIT_ROOM] = [
        msg(1, 11, "佐藤", "終了報告\n送信完了：40\n送信不可：2", "2026-10-14 18:00"),
        msg(2, 12, "鈴木", "終了報告\n送信完了：30\n送信不可：0", "2026-10-14 18:30"),
    ]
    ctx.sheets = FakeSheets({"佐藤": {"sent": 40, "unsendable": 2}, "鈴木": {"sent": 30, "unsendable": 0}})
    at("2026-10-14 23:30")
    req = daily_report.aggregate(db, ctx, date(2026, 10, 14))
    assert req.job_type == "daily_report" and req.status == "executed"  # MODE B
    assert "送信完了件数: 70件" in req.message and ctx.sheets.upserts
    assert len(db.scalars(select(DailyRecord)).all()) == 2

    ctx.cw.room_messages[RECRUIT_ROOM] = [msg(3, 11, "佐藤", "終了報告\n送信完了：50\n送信不可：0", "2026-10-15 18:00")]
    ctx.sheets = FakeSheets({"佐藤": {"sent": 48, "unsendable": 0}})
    at("2026-10-15 23:30")
    req2 = daily_report.aggregate(db, ctx, date(2026, 10, 15))
    assert req2.job_type == "daily_report_mismatch" and req2.status == "pending"
    assert ctx.sheets.upserts == []


# ---------------------------------------------------------------- 請求書
def test_invoices_exclude_unknown_rate(db, ctx):
    db.add_all([
        Worker(account_id=11, name="佐藤", rate_per_send=30, rate_per_appointment=5000, tax_category="exclusive_10"),
        Worker(account_id=12, name="鈴木"),
        DailyRecord(work_date=date(2026, 10, 5), account_id=11, name="佐藤", sent=100, appointments=1, confirmed=True),
        DailyRecord(work_date=date(2026, 10, 6), account_id=12, name="鈴木", sent=50, confirmed=True),
    ])
    db.flush()
    at("2026-10-31 21:00")
    req = invoices.run_if_month_end(db, ctx)
    assert [t["name"] for t in req.targets] == ["佐藤"]
    assert "支払予定総額: ¥8,800" in req.message
    assert any("鈴木" in w and "確認待ち" in w for w in req.warnings)
    approvals.decide(db, ctx, req, "approve", actor="admin")
    assert ctx.cw.uploads and ctx.cw.uploads[0][0] == 600
    assert {i.status for i in db.scalars(select(Invoice))} == {"sent", "needs_review"}
    at("2026-10-30 21:00")
    assert invoices.run_if_month_end(db, ctx) is None
