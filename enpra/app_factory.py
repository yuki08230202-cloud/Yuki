"""設定から AppContext を組み立てる。"""
from __future__ import annotations

import logging

from .chatwork import ChatworkClient
from .config import Settings
from .context import AppContext
from .notifier import Notifier

log = logging.getLogger(__name__)


def build_context(settings: Settings) -> AppContext:
    cw = ChatworkClient(settings.chatwork_api_token, dry_run=settings.dry_run)
    notify_token = settings.chatwork_notify_token or settings.chatwork_api_token
    notify_cw = ChatworkClient(notify_token, dry_run=settings.notify_dry_run) if notify_token else None
    sheets = None
    if settings.google_sheet_id:
        try:
            from .sheets import SheetsClient

            sheets = SheetsClient(
                settings.google_sheet_id,
                credentials_file=settings.google_service_account_file,
                dry_run=settings.dry_run,
                record_tab=settings.sheet_record_tab,
                worklog_tab=settings.sheet_worklog_tab,
            )
        except Exception:  # noqa: BLE001 - Sheets なしでも他機能は動かす
            log.exception("Google Sheets クライアントの初期化に失敗")
    llm = None
    if settings.llm_enabled:
        try:
            from .llm import AnswerDrafter

            llm = AnswerDrafter(settings.anthropic_model)
        except Exception:  # noqa: BLE001
            log.exception("Claude API クライアントの初期化に失敗")
    return AppContext(settings=settings, cw=cw, notifier=Notifier(settings, notify_cw), sheets=sheets, llm=llm)
