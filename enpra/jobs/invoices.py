"""月末請求書（初期 MODE A）。支払いは中島さんが行うため、自動振込は実装しない。

- 月末（毎日 21:00 のジョブで「明日が 1 日」の場合）に当月分を作成
- 単価・税区分が不明なワーカーは確認待ち（needs_review）とし承認対象から除外
- 承認後、中島さん宛のルーム（INVOICE_ROOM_ID）へ請求書ファイルを送付
"""
from __future__ import annotations

import html
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import approvals, clock
from ..approvals import executor, selected_targets
from ..context import AppContext
from ..models import ApprovalRequest, DailyRecord, Invoice, Worker
from ..outbound import send_once

TAX_LABELS = {"exclusive_10": "税抜(消費税10%加算)", "inclusive": "税込", "exempt": "非課税・免税"}


def compute(sent: int, appo: int, w: Worker) -> tuple[int | None, int | None, int | None, list[str]]:
    issues = []
    if w.rate_per_send is None:
        issues.append("送信単価が未設定")
    if appo and w.rate_per_appointment is None:
        issues.append("アポ単価が未設定")
    if w.tax_category not in TAX_LABELS:
        issues.append("税区分が未設定")
    if issues:
        return None, None, None, issues
    sub = sent * w.rate_per_send + appo * (w.rate_per_appointment or 0)
    if w.tax_category == "exclusive_10":
        tax = sub * 10 // 100
        total = sub + tax
    elif w.tax_category == "inclusive":
        tax = sub * 10 // 110
        total = sub
    else:
        tax, total = 0, sub
    return sub, tax, total, issues


def render_html(inv: Invoice, w: Worker) -> str:
    e = html.escape
    yen = lambda v: f"¥{v:,}" if v is not None else "-"  # noqa: E731
    return f"""<!doctype html><html lang="ja"><meta charset="utf-8"><title>請求書 {e(inv.month)} {e(w.name)}</title>
<body style="font-family:sans-serif;max-width:720px;margin:24px auto">
<h1>請求書</h1><p>対象月: {e(inv.month)}<br>請求者: {e(w.name)}</p>
<table border="1" cellpadding="6" style="border-collapse:collapse;width:100%">
<tr><th>項目</th><th>数量</th><th>単価</th></tr>
<tr><td>フォーム送信</td><td>{inv.sent_count}件</td><td>{yen(w.rate_per_send)}</td></tr>
<tr><td>アポイント獲得</td><td>{inv.appointment_count}件</td><td>{yen(w.rate_per_appointment)}</td></tr>
</table>
<p>小計: {yen(inv.subtotal)} / 消費税: {yen(inv.tax)}（{e(TAX_LABELS.get(w.tax_category or "", "未設定"))}）<br>
<strong>合計: {yen(inv.total)}</strong></p>
<p style="color:#666">※ 本書はシステムで作成した下書きです。源泉徴収の要否は別途ご確認ください。</p></body></html>"""


def build(db: Session, ctx: AppContext, month: str | None = None) -> ApprovalRequest | None:
    month = month or clock.today_jst().strftime("%Y-%m")
    y, m = (int(x) for x in month.split("-"))
    start = date(y, m, 1)
    end = (start + timedelta(days=32)).replace(day=1)
    rows = db.execute(
        select(DailyRecord.account_id, func.sum(DailyRecord.sent), func.sum(DailyRecord.appointments))
        .where(DailyRecord.work_date >= start, DailyRecord.work_date < end, DailyRecord.confirmed.is_(True))
        .group_by(DailyRecord.account_id)
    ).all()
    ready, review = [], []
    for aid, sent, appo in rows:
        w = db.scalar(select(Worker).where(Worker.account_id == aid))
        if w is None:
            continue
        sub, tax, total, issues = compute(int(sent or 0), int(appo or 0), w)
        inv = db.scalar(select(Invoice).where(Invoice.month == month, Invoice.worker_id == w.id))
        if inv is not None and inv.status == "sent":
            continue
        inv = inv or Invoice(month=month, worker_id=w.id)
        inv.sent_count, inv.appointment_count = int(sent or 0), int(appo or 0)
        inv.subtotal, inv.tax, inv.total, inv.issues = sub, tax, total, issues
        inv.status = "needs_review" if issues else "ready"
        db.add(inv)
        db.flush()
        inv.html = render_html(inv, w)
        (review if issues else ready).append((inv, w))

    if ctx.sheets is not None:
        try:
            sheet = ctx.sheets.monthly_totals(month)
            for inv, w in ready + review:
                s = sheet.get(str(w.account_id))
                if s and (s["sent"] != inv.sent_count or s["appointments"] != inv.appointment_count):
                    inv.issues = inv.issues + [f"シート集計と不一致（シート 送信{s['sent']}/アポ{s['appointments']}）"]
        except Exception as e:  # noqa: BLE001
            review_note = f"シートとの照合に失敗: {e}"
            for inv, _ in ready:
                inv.issues = inv.issues + [review_note]

    if not ready and not review:
        return None
    warnings = [f"{w.name}: {'、'.join(inv.issues)}（確認待ち・今回の送付対象外）" for inv, w in review]
    warnings += [f"{w.name}: {'、'.join(inv.issues)}" for inv, w in ready if inv.issues]
    lines = [
        f"対象月: {month}",
        f"対象ワーカー数: {len(ready)}名（確認待ち {len(review)}名）",
        f"総送信件数: {sum(i.sent_count for i, _ in ready)}件",
        f"総アポイント数: {sum(i.appointment_count for i, _ in ready)}件",
        f"支払予定総額: ¥{sum(i.total or 0 for i, _ in ready):,}",
        "個別請求書:",
        *[f"・{w.name}: ¥{inv.total:,} {ctx.settings.base_url}/invoices/{inv.id}" for inv, w in ready],
    ]
    db.flush()
    return approvals.dispatch(
        db,
        ctx,
        "invoice_send",
        title=f"{month} 請求書の中島さんへの送付",
        message="\n".join(lines),
        summary="承認後、請求書ファイルを中島さん宛ルームへ送付（自動振込は行いません）",
        targets=[{"key": str(inv.id), "name": w.name, "invoice_id": inv.id} for inv, w in ready],
        payload={"month": month},
        dedupe_key=f"invoice_send:{month}:{','.join(str(inv.id) for inv, _ in ready)}"[:200],
        warnings=warnings,
    )


def run_if_month_end(db: Session, ctx: AppContext) -> ApprovalRequest | None:
    today = clock.today_jst()
    if (today + timedelta(days=1)).day != 1:
        return None
    return build(db, ctx, today.strftime("%Y-%m"))


@executor("invoice_send")
def _execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> dict:
    room = ctx.settings.invoice_room_id
    if not room:
        raise RuntimeError("INVOICE_ROOM_ID（中島さん宛ルーム）が未設定です")
    sent = []
    for t in selected_targets(req):
        inv = db.get(Invoice, int(t["invoice_id"]))
        if inv is None or inv.status != "ready":
            continue
        fname = f"invoice_{inv.month}_{inv.worker.name}.html"
        send_once(db, key=f"invoice:{inv.month}:{inv.worker_id}", channel="chatwork", target=f"room:{room}",
                  body=fname, approval_id=req.id, dry_run=ctx.cw.dry_run,
                  send=lambda inv=inv, fname=fname: ctx.cw.upload_file(
                      room, fname, inv.html.encode("utf-8"),
                      f"中島さん\n{inv.month} {inv.worker.name}さんの請求書です（合計 ¥{inv.total:,}）"))
        if not ctx.cw.dry_run:
            inv.status = "sent"
        sent.append(inv.worker.name)
    db.flush()
    return {"summary": f"請求書を送付: {'、'.join(sent)}", "dry_run": ctx.cw.dry_run}
