"""ワーカーからの質問への回答（初期 MODE A）。

- 承認済みの過去回答（同じ質問）を最優先で再利用
- FAQ・マニュアルを検索し、根拠がある回答は job_type=qa_answer_faq（管理者が B/C に変更可能）
- 根拠なし、または報酬・契約などの判断を含む質問は job_type=qa_answer（MODE A 固定）
"""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import approvals
from ..approvals import executor
from ..chatwork import reply_header
from ..context import AppContext
from ..models import ApprovalRequest, FaqEntry, Question, SeenMessage
from ..outbound import send_once
from .participation import strip_tags

SENSITIVE_RE = re.compile(r"(報酬|単価|給料|給与|支払|振込|請求|契約|税|源泉|インボイス|解約|違約|損害)")
QUESTION_RE = re.compile(r"[?？]|教えて|わかりません|分かりません|質問|どう(すれば|したら)|でしょうか")
MATCH_THRESHOLD = 0.35


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", strip_tags(text)).lower()
    return re.sub(r"[\s、。!！?？「」『』()（）・,.]+", "", t)


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} or {s}


def similarity(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    return len(A & B) / len(A | B) if A and B else 0.0


def search_faq(db: Session, question: str, k: int = 3) -> list[dict]:
    nq = normalize(question)
    scored = []
    for f in db.scalars(select(FaqEntry)):
        s = 1.0 if f.normalized == nq else similarity(nq, f.normalized)
        scored.append({"id": f.id, "question": f.question, "answer": f.answer, "source": f.source, "score": round(s, 3)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return [x for x in scored[:k] if x["score"] > 0]


def add_faq(db: Session, question: str, answer: str, source: str = "manual") -> FaqEntry:
    f = FaqEntry(question=question, answer=answer, source=source, normalized=normalize(question))
    db.add(f)
    db.flush()
    return f


def is_question(body: str) -> bool:
    return bool(QUESTION_RE.search(strip_tags(body)))


def poll(db: Session, ctx: AppContext) -> list[int]:
    created = []
    for room_id in ctx.settings.question_room_ids:
        for m in ctx.cw.messages(room_id, force=True):
            mid = str(m["message_id"])
            if db.scalar(select(SeenMessage).where(SeenMessage.room_id == room_id, SeenMessage.message_id == mid,
                                                   SeenMessage.purpose == "qa")):
                continue
            db.add(SeenMessage(room_id=room_id, message_id=mid, purpose="qa"))
            req = handle_message(db, ctx, room_id, m)
            if req is not None:
                created.append(req.id)
    db.flush()
    return created


def handle_message(db: Session, ctx: AppContext, room_id: int, m: dict) -> ApprovalRequest | None:
    acc = m.get("account", {})
    aid = int(acc.get("account_id", 0))
    body = m.get("body", "")
    if not aid or aid == ctx.settings.admin_account_id or not is_question(body):
        return None
    if body.lstrip().startswith("終了報告"):
        return None
    mid = str(m["message_id"])
    if db.scalar(select(Question).where(Question.room_id == room_id, Question.message_id == mid)):
        return None
    q = Question(room_id=room_id, message_id=mid, account_id=aid, name=acc.get("name", ""), body=strip_tags(body))
    db.add(q)
    db.flush()
    return draft_answer(db, ctx, q)


def draft_answer(db: Session, ctx: AppContext, q: Question) -> ApprovalRequest:
    hits = search_faq(db, q.body)
    grounded = [h for h in hits if h["score"] >= MATCH_THRESHOLD]
    exact = next((h for h in grounded if h["score"] >= 0.999 and h["source"] == "approved_answer"), None)
    sensitive = bool(SENSITIVE_RE.search(q.body))
    warnings = []

    if exact:
        answer = exact["answer"]
        grounding = [exact]
    elif grounded:
        grounding = grounded
        answer = None
        if ctx.llm is not None:
            try:
                answer = ctx.llm.draft(q.body, grounded)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"AI 回答案の作成に失敗したため FAQ 本文を提示します: {e}")
        answer = answer or grounded[0]["answer"]
    else:
        grounding = []
        answer = "ご質問ありがとうございます。確認のうえ回答いたしますので、少々お待ちください。"
        warnings.append("FAQ・マニュアルに根拠が見つかりません。回答を修正してから承認してください")

    if sensitive:
        warnings.append("報酬・契約等に関わる質問のため、常に確認必須です")
    if answer.startswith("[要確認]"):
        warnings.append("AI が要確認と判断しました")
    job_type = "qa_answer_faq" if (grounding and not sensitive and not answer.startswith("[要確認]")) else "qa_answer"

    q.answer = answer
    q.grounding = grounding
    q.status = "drafted"
    req = approvals.dispatch(
        db,
        ctx,
        job_type,
        title=f"{q.name}さんの質問への回答",
        message=answer,
        summary=f"質問: {q.body[:300]}\n根拠: " + (" / ".join(f"FAQ#{g['id']}({g['score']})" for g in grounding) or "なし"),
        targets=[{"key": str(q.account_id), "name": q.name}],
        payload={"question_id": q.id},
        dedupe_key=f"qa:{q.room_id}:{q.message_id}",
        warnings=warnings,
        force_confirm=job_type == "qa_answer",
    )
    q.approval_id = req.id
    db.flush()
    return req


def _execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> dict:
    q = db.get(Question, int(req.payload["question_id"]))
    body = reply_header(q.account_id, q.room_id, q.message_id, q.name) + req.message
    send_once(db, key=f"qa_answer:{q.room_id}:{q.message_id}", channel="chatwork", target=f"room:{q.room_id}",
              body=body, approval_id=req.id, dry_run=ctx.cw.dry_run,
              send=lambda: ctx.cw.post_message(q.room_id, body))
    q.answer = req.message
    q.status = "answered"
    # 承認済み回答として蓄積し、同じ質問で優先利用する
    nq = normalize(q.body)
    existing = db.scalar(select(FaqEntry).where(FaqEntry.normalized == nq, FaqEntry.source == "approved_answer"))
    if existing:
        existing.answer = req.message
    else:
        add_faq(db, q.body, req.message, source="approved_answer")
    db.flush()
    return {"summary": f"{q.name}さんへ回答しました", "dry_run": ctx.cw.dry_run}


executor("qa_answer")(_execute)
executor("qa_answer_faq")(_execute)
