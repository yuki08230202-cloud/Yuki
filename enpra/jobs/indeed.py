"""Indeed 応募者への案内（既存約4名を含む未対応者）。

API 検証結果（docs/feasibility.md 参照）:
  Indeed の公開 API は ATS パートナー向け（Indeed Apply / Job Sync / Disposition Sync 等）で、
  事前の書面承認が必要。雇用主が応募者へメッセージを送る公開 API は確認できなかった。
  → 当面は「取得=管理画面への CSV 取込」「送信=承認後に管理者が Indeed 上で手動送信（文面コピー）」
    という規約に適合する半自動方式で実装し、API 権限が得られたら IndeedSource / 送信部分を差し替える。

処理手順:
  1. 応募者の取得（CSV 取込 / 将来 API）
  2. 過去の送信履歴を確認（Applicant.status / OutboundMessage）
  3. 未対応者を抽出
  4. 既存テンプレートを読み込み
  5. Chatwork ID を設定値（i4snxm1oompx）に差し替え
  6. 管理者へ通知（一括・個別承認）
  7. 承認後に送信（現状: 手動送信待ちとして文面を用意）
  8. 結果を記録（管理者が「送信済み」を押して確定）
"""
from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import approvals, clock
from ..approvals import executor, selected_targets
from ..context import AppContext
from ..models import ApprovalRequest, AuditLog, Applicant, OutboundMessage
from ..templates_text import render, replace_chatwork_id

CSV_COLUMNS = ("external_id", "name", "job_title", "applied_at", "contact_url")


def import_csv(db: Session, text: str) -> dict:
    """CSV（ヘッダ: external_id,name,job_title,applied_at,contact_url）を取り込む。"""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    added, updated = 0, 0
    for row in reader:
        ext = (row.get("external_id") or "").strip()
        name = (row.get("name") or "").strip()
        if not ext or not name:
            continue
        a = db.scalar(select(Applicant).where(Applicant.external_id == ext))
        if a is None:
            db.add(Applicant(
                external_id=ext,
                name=name,
                job_title=(row.get("job_title") or "").strip() or None,
                applied_at=(row.get("applied_at") or "").strip() or None,
                contact_url=(row.get("contact_url") or "").strip() or None,
            ))
            added += 1
        else:
            a.name = name
            a.job_title = (row.get("job_title") or a.job_title or "").strip() or None
            a.contact_url = (row.get("contact_url") or a.contact_url or "").strip() or None
            updated += 1
    db.flush()
    return {"added": added, "updated": updated}


def already_guided(db: Session, a: Applicant) -> bool:
    if a.status in ("guided", "approved_manual_send"):
        return True
    om = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == f"indeed_guide:{a.external_id}"))
    return om is not None and om.status in ("sent", "manual_pending")


def build_template(db: Session, ctx: AppContext) -> tuple[str, list[str]]:
    warnings = []
    tpl = render("indeed_guide", db, chatwork_id=ctx.settings.admin_chatwork_id, admin_name=ctx.settings.admin_name)
    tpl, n = replace_chatwork_id(tpl, ctx.settings.admin_chatwork_id)
    if ctx.settings.admin_chatwork_id not in tpl:
        warnings.append("テンプレートに Chatwork ID の記載が見つかりません。文面を確認してください")
    return tpl, warnings


def prepare_guides(db: Session, ctx: AppContext) -> ApprovalRequest | None:
    pending = [a for a in db.scalars(select(Applicant).where(Applicant.status == "new")) if not already_guided(db, a)]
    if not pending:
        return None
    tpl, warnings = build_template(db, ctx)
    targets = []
    for a in pending:
        a.status = "awaiting_approval"
        targets.append({"key": a.external_id, "name": a.name, "job_title": a.job_title or "", "url": a.contact_url or ""})
    warnings.append("Indeed には送信 API がないため、承認後に管理画面の文面を Indeed のメッセージ画面へ貼り付けて送信してください")
    keys = ",".join(sorted(t["key"] for t in targets))
    return approvals.dispatch(
        db,
        ctx,
        "indeed_guide",
        title=f"Indeed応募者 {len(targets)}名への案内",
        message=tpl,
        summary="未対応の応募者へ Chatwork 登録・コンタクト申請の案内（{name}/{job_title} は各自に置換）",
        targets=targets,
        dedupe_key=f"indeed_guide:{clock.today_jst().isoformat()}:{keys}"[:200],
        url=f"{ctx.settings.base_url}/indeed",
        warnings=warnings,
    )


def personalize(template: str, t: dict) -> str:
    return template.replace("{name}", t["name"]).replace("{job_title}", t.get("job_title") or "求人")


@executor("indeed_guide")
def _execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> dict:
    chosen = selected_targets(req)
    chosen_keys = {t["key"] for t in chosen}
    prepared = []
    for t in chosen:
        a = db.scalar(select(Applicant).where(Applicant.external_id == t["key"]))
        if a is None or a.status == "guided":
            continue
        key = f"indeed_guide:{a.external_id}"
        om = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == key))
        if om is None:
            om = OutboundMessage(idempotency_key=key, channel="indeed_manual", target=a.name, body="", status="manual_pending")
            db.add(om)
        om.body = personalize(req.message, t)
        om.approval_id = req.id
        om.status = "manual_pending"
        a.status = "approved_manual_send"
        prepared.append(a.name)
    # 承認対象から外した応募者は未対応に戻す
    for t in req.targets:
        if t["key"] not in chosen_keys:
            a = db.scalar(select(Applicant).where(Applicant.external_id == t["key"]))
            if a and a.status == "awaiting_approval":
                a.status = "new"
    db.flush()
    return {
        "summary": f"{len(prepared)}名分の案内文を用意しました（Indeed 上での送信待ち）: {'、'.join(prepared) or '-'}\n"
        f"送信後に {ctx.settings.base_url}/indeed で「送信済み」を押してください",
        "prepared": prepared,
    }


def mark_sent(db: Session, applicant_id: int, actor: str) -> Applicant:
    a = db.get(Applicant, applicant_id)
    if a is None:
        raise ValueError("応募者が見つかりません")
    om = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == f"indeed_guide:{a.external_id}"))
    if om is None or om.status != "manual_pending":
        raise ValueError("承認済みの案内文がありません（先に承認が必要です）")
    om.status = "sent"
    a.status = "guided"
    a.guided_at = clock.utcnow()
    db.add(AuditLog(actor=actor, action="indeed_mark_sent", detail={"applicant": a.external_id}))
    db.flush()
    return a
