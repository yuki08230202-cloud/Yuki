"""Claude API による FAQ 根拠付き回答案の作成（任意機能。LLM_ENABLED=true で有効）。

回答案は必ず承認フローに乗せる。FAQ に根拠がない場合は「根拠なし」として返し、
呼び出し側で MODE A 固定の業務（qa_answer）に回す。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SYSTEM = (
    "あなたはフォーム営業ワーカーからの質問に回答する事務局アシスタントです。"
    "提供された FAQ・マニュアルの抜粋だけを根拠に、丁寧な日本語で簡潔に回答案を作成してください。"
    "抜粋に答えがない場合や、報酬・単価・契約・支払い・税金に関する判断が必要な場合は、推測で答えず、"
    "回答の先頭に「[要確認]」と書き、管理者が確認する旨だけを伝える文面にしてください。"
)


class AnswerDrafter:
    def __init__(self, model: str, client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model

    def draft(self, question: str, excerpts: list[dict]) -> str | None:
        ctx = "\n\n".join(f"[FAQ {i + 1}]\nQ: {e['question']}\nA: {e['answer']}" for i, e in enumerate(excerpts))
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=SYSTEM,
            messages=[{"role": "user", "content": f"FAQ・マニュアル抜粋:\n{ctx or '(該当なし)'}\n\nワーカーからの質問:\n{question}"}],
        )
        if resp.stop_reason == "refusal":
            log.warning("回答案の作成が拒否されました")
            return None
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return text or None
