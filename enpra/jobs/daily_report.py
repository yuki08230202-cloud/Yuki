"""日報（終了報告）の読み取りと実績集計（初期 MODE B）。

不一致（報告とシートの件数差）がある場合は自動確定せず、MODE A 固定の
daily_report_mismatch として管理者確認に回す。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import approvals, clock
from ..approvals import executor
from ..context import AppContext
from ..models import ApprovalRequest, DailyRecord, Participation
from .participation import strip_tags

_SENT_RE = re.compile(r"送信完了\s*[:：]?\s*(\d+)")
_NG_RE = re.compile(r"送信不可\s*[:：]?\s*(\d+)")
_APPO_RE = re.compile(r"アポ(?:イント)?(?:獲得)?(?:数)?\s*[:：]?\s*(\d+)")


def parse_report(body: str) -> dict | None:
    text = strip_tags(body)
    if "終了報告" not in text:
        return None
    sent, ng = _SENT_RE.search(text), _NG_RE.search(text)
    if not sent and not ng:
        return None
    appo = _APPO_RE.search(text)
    return {"sent": int(sent.group(1)) if sent else 0, "unsendable": int(ng.group(1)) if ng else 0,
            "appointments": int(appo.group(1)) if appo else 0}


def collect_reports(ctx: AppContext, work_date: date) -> dict[int, dict]:
    room = ctx.settings.effective_report_room_id
    start = clock.at_jst(work_date, "00:00")
    end = start + timedelta(days=1, hours=6)  # 深夜〜翌朝 6 時までの報告も当日分とみなす
    reports: dict[int, dict] = {}
    for m in sorted(ctx.cw.messages(room, force=True), key=lambda x: x.get("send_time", 0)):
        t = datetime.fromtimestamp(m.get("send_time", 0), tz=timezone.utc)
        if not (start <= t < end):
            continue
        r = parse_report(m.get("body", ""))
        if r is None:
            continue
        acc = m.get("account", {})
        r.update(account_id=int(acc.get("account_id", 0)), name=acc.get("name", ""), message_id=str(m["message_id"]))
        reports[r["account_id"]] = r  # 同一人物は最新の報告を採用
    return reports


def aggregate(db: Session, ctx: AppContext, work_date: date | None = None) -> ApprovalRequest:
    if work_date is None:
        # 午前中の実行は前日分（深夜の報告を取り込むため翌朝 7:00 実行を想定）
        now = clock.now_jst()
        work_date = now.date() if now.hour >= 12 else now.date() - timedelta(days=1)
    reports = collect_reports(ctx, work_date)
    expected = {p.account_id: p.name for p in db.scalars(select(Participation).where(Participation.target_date == work_date))}
    missing = [n for aid, n in expected.items() if aid not in reports]

    mismatches = []
    if ctx.sheets is not None:
        try:
            log = ctx.sheets.worklog_counts(work_date.isoformat())
        except Exception as e:  # noqa: BLE001
            log = None
            mismatches.append(f"シートの照合に失敗: {e}")
        if log is not None:
            for r in reports.values():
                s = log.get(r["name"])
                if s is None:
                    mismatches.append(f"{r['name']}: シートに作業記録がありません")
                elif s["sent"] != r["sent"] or s["unsendable"] != r["unsendable"]:
                    mismatches.append(f"{r['name']}: 報告 完了{r['sent']}/不可{r['unsendable']} ≠ シート 完了{s['sent']}/不可{s['unsendable']}")

    total_sent = sum(r["sent"] for r in reports.values())
    total_ng = sum(r["unsendable"] for r in reports.values())
    lines = [
        f"集計日: {clock.fmt_date_ja(work_date)}",
        f"稼働人数: {len(reports)}名（参加表明 {len(expected)}名）",
        f"送信完了件数: {total_sent}件",
        f"送信不可件数: {total_ng}件",
        "ワーカー別実績:",
        *[f"・{r['name']}: 完了{r['sent']} / 不可{r['unsendable']} / アポ{r['appointments']}" for r in reports.values()],
        f"未提出者: {'、'.join(missing) or 'なし'}",
        f"数値不一致: {'あり' if mismatches else 'なし'}",
        *[f"・{m}" for m in mismatches],
    ]
    job_type = "daily_report_mismatch" if mismatches else "daily_report"
    return approvals.dispatch(
        db,
        ctx,
        job_type,
        title=f"{clock.fmt_date_ja(work_date)} 実績集計の反映",
        message="\n".join(lines),
        summary=f"「{ctx.settings.sheet_record_tab}」タブへ反映（更新前の値はバックアップ）",
        targets=[{"key": str(r["account_id"]), "name": r["name"]} for r in reports.values()],
        payload={"work_date": work_date.isoformat(), "reports": list(reports.values()), "missing": missing,
                 "mismatches": mismatches},
        dedupe_key=f"daily_report:{work_date.isoformat()}",
        warnings=mismatches + ([f"未提出: {'、'.join(missing)}"] if missing else []),
        force_confirm=bool(mismatches),
    )


def _execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> dict:
    work_date = date.fromisoformat(req.payload["work_date"])
    keys = set(req.selected_keys) if req.selected_keys is not None else None
    records = [r for r in req.payload["reports"] if keys is None or str(r["account_id"]) in keys]
    for r in records:
        d = db.scalar(select(DailyRecord).where(DailyRecord.work_date == work_date, DailyRecord.account_id == r["account_id"]))
        d = d or DailyRecord(work_date=work_date, account_id=r["account_id"])
        d.name, d.sent, d.unsendable = r["name"], r["sent"], r["unsendable"]
        d.appointments, d.source_message_id, d.confirmed = r.get("appointments", 0), r.get("message_id"), True
        db.add(d)
    changes = []
    if ctx.sheets is not None and records:
        changes = ctx.sheets.upsert_daily_records(db, work_date.isoformat(), records, req.id)
    db.flush()
    return {"summary": req.message, "sheet_changes": [c.id for c in changes],
            "dry_run": ctx.sheets is None or ctx.sheets.dry_run}


executor("daily_report")(_execute)
executor("daily_report_mismatch")(_execute)
