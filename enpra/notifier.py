"""管理者通知。Chatwork → 失敗時メール → それも失敗したら DB 記録（管理画面に表示）。

通知は管理者本人のトークンで送ると「自分の発言」になり通知が鳴らないため、
CHATWORK_NOTIFY_TOKEN に通知専用 Bot アカウントのトークンを設定することを推奨する。
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from .chatwork import ChatworkClient
from .config import Settings
from .models import Notification

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings, chatwork: ChatworkClient | None, smtp_factory=smtplib.SMTP):
        self.s = settings
        self.cw = chatwork
        self._smtp_factory = smtp_factory

    def notify(self, db: Session, kind: str, title: str, body: str) -> Notification:
        text = self._format(title, body)
        if self.s.notify_dry_run:
            log.info("[NOTIFY_DRY_RUN] %s\n%s", title, body)
            n = Notification(kind=kind, body=text, channel="none", status="dry_run")
            db.add(n)
            db.flush()
            return n

        errors: list[str] = []
        if self.cw and self.s.admin_notify_room_id:
            try:
                self.cw.post_message(self.s.admin_notify_room_id, text)
                n = Notification(kind=kind, body=text, channel="chatwork", status="sent")
                db.add(n)
                db.flush()
                return n
            except Exception as e:  # noqa: BLE001 - 代替経路に切り替える
                errors.append(f"chatwork: {e}")
                log.exception("Chatwork 通知に失敗。メールに切り替えます")
        else:
            errors.append("chatwork: 通知ルーム未設定")

        if self.s.smtp_host and self.s.admin_email:
            try:
                self._send_email(f"[エンプラ] {title}", f"{body}\n\n(Chatwork 通知失敗: {'; '.join(errors)})")
                n = Notification(kind=kind, body=text, channel="email", status="sent", error="; ".join(errors))
                db.add(n)
                db.flush()
                return n
            except Exception as e:  # noqa: BLE001
                errors.append(f"email: {e}")
                log.exception("メール通知にも失敗")

        n = Notification(kind=kind, body=text, channel="none", status="failed", error="; ".join(errors))
        db.add(n)
        db.flush()
        return n

    def _format(self, title: str, body: str) -> str:
        to = f"[To:{self.s.admin_account_id}]{self.s.admin_name}さん\n" if self.s.admin_account_id else ""
        return f"{to}[info][title]{title}[/title]{body}[/info]"

    def _send_email(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.s.smtp_from or self.s.smtp_user
        msg["To"] = self.s.admin_email
        msg.set_content(body)
        with self._smtp_factory(self.s.smtp_host, self.s.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)
