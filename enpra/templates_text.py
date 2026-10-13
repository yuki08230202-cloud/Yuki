"""メッセージテンプレートの読み込みと差し込み。

テンプレートは enpra/messages/*.txt に置き、運用者が編集できる。
"""
from __future__ import annotations

import re
from pathlib import Path

MSG_DIR = Path(__file__).parent / "messages"


def load(name: str, db=None) -> str:
    if db is not None:
        from .models import TextTemplate

        t = db.get(TextTemplate, name)
        if t is not None:
            return t.body
    return (MSG_DIR / f"{name}.txt").read_text(encoding="utf-8")


class _Keep(dict):
    """未知のプレースホルダーは {key} のまま残す（既存テンプレートの取り込み用）。"""

    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, db=None, /, **kw) -> str:
    return load(template, db).format_map(_Keep(kw)).strip()


_CW_ID_RE = re.compile(r"(Chatwork\s*ID|チャットワーク\s*ID|ChatworkID)(\s*[:：]\s*)([A-Za-z0-9_\-\.]+)", re.I)


def replace_chatwork_id(text: str, new_id: str) -> tuple[str, int]:
    """既存テンプレート中の Chatwork ID 表記を new_id に差し替える。(結果, 置換数)"""
    return _CW_ID_RE.subn(lambda m: f"{m.group(1)}{m.group(2)}{new_id}", text)
