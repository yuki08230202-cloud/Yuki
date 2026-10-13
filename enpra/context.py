from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .chatwork import ChatworkClient
from .config import Settings
from .notifier import Notifier


@dataclass
class AppContext:
    settings: Settings
    cw: ChatworkClient            # ワーカー向け操作（管理者アカウント）
    notifier: Notifier
    sheets: Any = None            # enpra.sheets.SheetsClient | None
    llm: Any = None               # enpra.llm.AnswerDrafter | None
