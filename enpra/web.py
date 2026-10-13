"""Web 管理画面（スマホ対応）・承認リンク・定期ジョブ・Chatwork Webhook。

セキュリティ:
- 承認リンク /a/{token} は管理者ログインが必須。リンクだけでは何も実行できない。
- トークンは一度限り・有効期限付き。DB にはハッシュのみ保存。
- 状態を変える操作は POST + CSRF トークン。
- /jobs/* は X-Job-Secret ヘッダ（Cloud Scheduler から送る）で認証。
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import desc, select

from . import approvals, clock, db as dbm
from .app_factory import build_context
from .config import Settings
from .context import AppContext
from .jobs import indeed, qa
from .jobs_runner import JOBS, run_job
from .models import (
    ApprovalRequest,
    Applicant,
    AuditLog,
    ContactSnapshot,
    FaqEntry,
    Invoice,
    JobSetting,
    Notification,
    SeenMessage,
    TextTemplate,
)
from .security import csrf_token, token_hash, verify_chatwork_signature, verify_password

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.globals.update(fmt_dt=clock.fmt_dt_ja, MODES=approvals.MODES, JOB_TYPES=approvals.JOB_TYPES)
COOKIE = "enpra_session"
EDITABLE_TEMPLATES = {"indeed_guide": "Indeed応募者への案内", "recruit": "毎日の募集文",
                      "individual_request": "個別依頼文", "welcome": "新規ワーカーへの挨拶"}


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.session_secret:
        settings.session_secret = secrets.token_hex(32)
        log.warning("SESSION_SECRET が未設定です。再起動でログインが切れます")
    dbm.init_db(settings.database_url)
    ctx = ctx or build_context(settings)
    with dbm.session_scope() as s:
        approvals.ensure_job_settings(s)

    app = FastAPI(title="エンプラ ワーカー管理", docs_url=None, redoc_url=None)
    signer = URLSafeTimedSerializer(settings.session_secret, salt="enpra-session")
    login_failures: dict[str, list[float]] = {}

    # ------------------------------------------------------------ 認証
    def current_session(request: Request) -> dict | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            return signer.loads(raw, max_age=settings.session_max_age_sec)
        except BadSignature:
            return None

    def require_admin(request: Request) -> dict:
        sess = current_session(request)
        if not sess:
            nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            raise HTTPException(status_code=303, headers={"Location": f"/login?next={nxt}"})
        return sess

    def check_csrf(sess: dict, token: str) -> None:
        if not hmac.compare_digest(csrf_token(settings.session_secret, sess["sid"]), token or ""):
            raise HTTPException(400, "CSRF トークンが不正です。画面を再読み込みしてください")

    def render(request: Request, template: str, sess: dict | None = None, /, **kw):
        kw.update(request=request, settings=settings, sess=sess,
                  csrf=csrf_token(settings.session_secret, sess["sid"]) if sess else "")
        return TEMPLATES.TemplateResponse(request, template, kw)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "dry_run": settings.dry_run, "notify_dry_run": settings.notify_dry_run}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return render(request, "login.html", next=next, error=None)

    @app.post("/login")
    def login(request: Request, password: str = Form(...), next: str = Form("/")):
        ip = request.client.host if request.client else "?"
        now = time.time()
        fails = [t for t in login_failures.get(ip, []) if now - t < 900]
        if len(fails) >= 5:
            return render(request, "login.html", next=next, error="試行回数が多すぎます。15分後に再度お試しください")
        if not settings.admin_password_hash or not verify_password(password, settings.admin_password_hash):
            fails.append(now)
            login_failures[ip] = fails
            return render(request, "login.html", next=next, error="パスワードが違います")
        login_failures.pop(ip, None)
        if not next.startswith("/") or next.startswith("//"):
            next = "/"
        resp = RedirectResponse(next, status_code=303)
        resp.set_cookie(COOKIE, signer.dumps({"sid": secrets.token_hex(16), "user": "admin"}),
                        max_age=settings.session_max_age_sec, httponly=True,
                        secure=settings.base_url.startswith("https"), samesite="lax")
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # ------------------------------------------------------------ ダッシュボード
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, sess: dict = Depends(require_admin)):
        with dbm.session_scope() as s:
            waiting = s.scalars(select(ApprovalRequest).where(ApprovalRequest.status.in_(("pending", "held", "expired")))
                                .order_by(desc(ApprovalRequest.created_at))).all()
            scheduled = s.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "approved")).all()
            recent = s.scalars(select(ApprovalRequest).where(ApprovalRequest.status.in_(("executed", "failed", "skipped")))
                               .order_by(desc(ApprovalRequest.id)).limit(20)).all()
            snap = s.scalar(select(ContactSnapshot).order_by(desc(ContactSnapshot.id)).limit(1))
            failed_notices = s.scalars(select(Notification).where(Notification.status == "failed")
                                       .order_by(desc(Notification.id)).limit(5)).all()
            return render(request, "dashboard.html", sess, waiting=waiting, scheduled=scheduled, recent=recent,
                          snap=snap, failed_notices=failed_notices)

    # ------------------------------------------------------------ 承認
    @app.get("/a/{token}", response_class=HTMLResponse)
    def approval_by_token(token: str, request: Request, action: str | None = None,
                          sess: dict = Depends(require_admin)):
        with dbm.session_scope() as s:
            req = approvals.find_by_token(s, token)
            if req is None:
                old = s.scalar(select(ApprovalRequest).where(ApprovalRequest.token_hash == token_hash(token)))
                return render(request, "message.html", sess, title="リンクが無効です",
                              body="このリンクは使用済みか、有効期限が切れています。",
                              link=f"/approvals/{old.id}" if old else "/")
            return render(request, "approval.html", sess, req=req, token=token, focus=action, now=clock.utcnow())

    @app.get("/approvals/{rid}", response_class=HTMLResponse)
    def approval_detail(rid: int, request: Request, sess: dict = Depends(require_admin)):
        with dbm.session_scope() as s:
            req = s.get(ApprovalRequest, rid)
            if req is None:
                raise HTTPException(404)
            audits = s.scalars(select(AuditLog).where(AuditLog.approval_id == rid).order_by(AuditLog.id)).all()
            return render(request, "approval.html", sess, req=req, token="", focus=None, audits=audits,
                          now=clock.utcnow())

    @app.post("/approvals/{rid}/decide")
    async def decide(rid: int, request: Request, sess: dict = Depends(require_admin)):
        form = await request.form()
        check_csrf(sess, form.get("csrf", ""))
        action = form.get("action", "")
        with dbm.session_scope() as s:
            req = s.get(ApprovalRequest, rid)
            if req is None:
                raise HTTPException(404)
            token = form.get("token") or ""
            if token and approvals.find_by_token(s, token) is not req:
                return render(request, "message.html", sess, title="リンクが無効です",
                              body="使用済みまたは期限切れです。", link=f"/approvals/{rid}")
            selected = form.getlist("selected") if req.targets else None
            if action == "automate" and form.get("confirm_automate") != "yes":
                return render(request, "message.html", sess, title="確認が必要です",
                              body="「今後自動化」には確認チェックが必要です。", link=f"/approvals/{rid}")
            try:
                approvals.decide(
                    s, ctx, req, action, actor=sess["user"],
                    edited_message=form.get("message") if action == "edit_approve" else None,
                    selected_keys=list(selected) if selected is not None else None,
                    automate_mode=form.get("automate_mode") or None,
                )
            except approvals.ApprovalError as e:
                return render(request, "message.html", sess, title="操作できません", body=str(e),
                              link=f"/approvals/{rid}")
            return RedirectResponse(f"/approvals/{rid}", status_code=303)

    @app.post("/approvals/{rid}/retry")
    def retry(rid: int, request: Request, csrf: str = Form(""), sess: dict = Depends(require_admin)):
        check_csrf(sess, csrf)
        with dbm.session_scope() as s:
            req = s.get(ApprovalRequest, rid)
            try:
                approvals.retry_failed(s, ctx, req, sess["user"])
            except approvals.ApprovalError as e:
                return render(request, "message.html", sess, title="操作できません", body=str(e), link=f"/approvals/{rid}")
        return RedirectResponse(f"/approvals/{rid}", status_code=303)

    @app.post("/approvals/{rid}/reissue")
    def reissue(rid: int, request: Request, csrf: str = Form(""), sess: dict = Depends(require_admin)):
        check_csrf(sess, csrf)
        with dbm.session_scope() as s:
            req = s.get(ApprovalRequest, rid)
            try:
                approvals.reissue_link(s, ctx, req, "管理画面から承認リンクを再発行しました。")
            except approvals.ApprovalError as e:
                return render(request, "message.html", sess, title="操作できません", body=str(e), link=f"/approvals/{rid}")
        return RedirectResponse(f"/approvals/{rid}", status_code=303)

    # ------------------------------------------------------------ モード設定
    @app.get("/modes", response_class=HTMLResponse)
    def modes(request: Request, sess: dict = Depends(require_admin)):
        with dbm.session_scope() as s:
            rows = s.scalars(select(JobSetting).order_by(JobSetting.job_type)).all()
            return render(request, "modes.html", sess, rows=rows)

    @app.post("/modes/{job_type}")
    def set_mode(job_type: str, request: Request, mode: str = Form(...), csrf: str = Form(""),
                 sess: dict = Depends(require_admin)):
        check_csrf(sess, csrf)
        with dbm.session_scope() as s:
            try:
                approvals.change_mode(s, job_type, mode, sess["user"])
            except approvals.ApprovalError as e:
                return render(request, "message.html", sess, title="変更できません", body=str(e), link="/modes")
        return RedirectResponse("/modes", status_code=303)

    # ------------------------------------------------------------ Indeed
    @app.get("/indeed", response_class=HTMLResponse)
    def indeed_page(request: Request, sess: dict = Depends(require_admin)):
        from .models import OutboundMessage

        with dbm.session_scope() as s:
            rows = s.scalars(select(Applicant).order_by(Applicant.id)).all()
            drafts = {om.idempotency_key.split(":", 1)[1]: om.body for om in
                      s.scalars(select(OutboundMessage).where(OutboundMessage.channel == "indeed_manual"))}
            return render(request, "indeed.html", sess, rows=rows, drafts=drafts, columns=",".join(indeed.CSV_COLUMNS))

    @app.post("/indeed/import")
    def indeed_import(request: Request, csv_text: str = Form(...), csrf: str = Form(""),
                      sess: dict = Depends(require_admin)):
        check_csrf(sess, csrf)
        with dbm.session_scope() as s:
            res = indeed.import_csv(s, csv_text)
            s.add(AuditLog(actor=sess["user"], action="indeed_import", detail=res))
        return RedirectResponse("/indeed", status_code=303)

    @app.post("/indeed/prepare")
    def indeed_prepare(request: Request, csrf: str = Form(""), sess: dict = Depends(require_admin)):
        check_csrf(sess, csrf)
        with dbm.session_scope() as s:
            req = indeed.prepare_guides(s, ctx)
        return RedirectResponse(f"/approvals/{req.id}" if req else "/indeed", status_code=303)

    @app.post("/indeed/{aid}/sent")
    def indeed_sent(aid: int, request: Request, csrf: str = Form(""), sess: dict = Depends(require_admin)):
        check_csrf(sess, csrf)
        with dbm.session_scope() as s:
            try:
                indeed.mark_sent(s, aid, sess["user"])
            except ValueError as e:
                return render(request, "message.html", sess, title="操作できません", body=str(e), link="/indeed")
        return RedirectResponse("/indeed", status_code=303)

    # ------------------------------------------------------------ FAQ / テンプレート / 請求書
    @app.get("/faq", response_class=HTMLResponse)
    def faq_page(request: Request, sess: dict = Depends(require_admin)):
        with dbm.session_scope() as s:
            return render(request, "faq.html", sess, rows=s.scalars(select(FaqEntry).order_by(desc(FaqEntry.id))).all())

    @app.post("/faq")
    def faq_add(request: Request, question: str = Form(...), answer: str = Form(...), csrf: str = Form(""),
                sess: dict = Depends(require_admin)):
        check_csrf(sess, csrf)
        with dbm.session_scope() as s:
            qa.add_faq(s, question, answer, source="faq")
        return RedirectResponse("/faq", status_code=303)

    @app.get("/templates/{name}", response_class=HTMLResponse)
    def template_page(name: str, request: Request, sess: dict = Depends(require_admin)):
        if name not in EDITABLE_TEMPLATES:
            raise HTTPException(404)
        from .templates_text import load

        with dbm.session_scope() as s:
            return render(request, "template_edit.html", sess, name=name, label=EDITABLE_TEMPLATES[name],
                          body=load(name, s))

    @app.post("/templates/{name}")
    def template_save(name: str, request: Request, body: str = Form(...), csrf: str = Form(""),
                      sess: dict = Depends(require_admin)):
        check_csrf(sess, csrf)
        if name not in EDITABLE_TEMPLATES:
            raise HTTPException(404)
        with dbm.session_scope() as s:
            t = s.get(TextTemplate, name) or TextTemplate(name=name, body="")
            t.body = body
            t.updated_at = clock.utcnow()
            s.add(t)
            s.add(AuditLog(actor=sess["user"], action="template_update", detail={"name": name}))
        return RedirectResponse(f"/templates/{name}", status_code=303)

    @app.get("/invoices/{iid}", response_class=HTMLResponse)
    def invoice_view(iid: int, sess: dict = Depends(require_admin)):
        with dbm.session_scope() as s:
            inv = s.get(Invoice, iid)
            if inv is None:
                raise HTTPException(404)
            return HTMLResponse(inv.html)

    # ------------------------------------------------------------ 定期ジョブ
    @app.post("/jobs/{name}")
    def job(name: str, x_job_secret: str = Header(default="")):
        if not settings.job_secret or not hmac.compare_digest(settings.job_secret, x_job_secret):
            raise HTTPException(401, "invalid job secret")
        if name not in JOBS:
            raise HTTPException(404, "unknown job")
        try:
            with dbm.session_scope() as s:
                result = run_job(s, ctx, name)
            return JSONResponse({"job": name, "result": json.loads(json.dumps(result, default=str))})
        except Exception as e:
            log.exception("job %s failed", name)
            try:
                with dbm.session_scope() as s:
                    ctx.notifier.notify(s, "error", f"【ジョブ失敗】{JOBS[name][0]}", f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                log.exception("失敗通知も送れませんでした")
            # 5xx を返して Cloud Scheduler の再試行に任せる
            raise HTTPException(500, f"job failed: {e}") from e

    # ------------------------------------------------------------ Chatwork Webhook
    @app.post("/webhook/chatwork")
    async def chatwork_webhook(request: Request):
        body = await request.body()
        sig = request.headers.get("x-chatworkwebhooksignature", "")
        if not verify_chatwork_signature(settings.chatwork_webhook_token, body, sig):
            raise HTTPException(401, "invalid signature")
        data = json.loads(body or b"{}")
        ev = data.get("webhook_event") or {}
        room_id = int(ev.get("room_id") or 0)
        if data.get("webhook_event_type") in ("message_created", "mention_to_me") and room_id in settings.question_room_ids:
            msg = {"message_id": ev.get("message_id"), "body": ev.get("body", ""),
                   "account": {"account_id": ev.get("account_id") or ev.get("from_account_id"), "name": ""}}
            with dbm.session_scope() as s:
                seen = s.scalar(select(SeenMessage).where(SeenMessage.room_id == room_id,
                                                          SeenMessage.message_id == str(msg["message_id"]),
                                                          SeenMessage.purpose == "qa"))
                if not seen:
                    s.add(SeenMessage(room_id=room_id, message_id=str(msg["message_id"]), purpose="qa"))
                    try:
                        full = ctx.cw.message(room_id, str(msg["message_id"]))
                        msg = full or msg
                    except Exception:  # noqa: BLE001 - 名前が取れなくても処理は続ける
                        log.exception("メッセージ詳細の取得に失敗")
                    qa.handle_message(s, ctx, room_id, msg)
        return {"ok": True}

    app.state.ctx = ctx
    app.state.settings = settings
    return app
