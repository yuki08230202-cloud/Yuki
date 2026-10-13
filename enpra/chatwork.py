"""Chatwork API v2 クライアント。

- 読み取り系は DRY_RUN でも実行する（検証のため）。
- 書き込み系は `dry_run=True` のとき送信せず、ダミー応答を返す。
- 429 / 5xx は指数バックオフで再試行する（レート制限ヘッダ x-ratelimit-reset を尊重）。

注意（公式 API 仕様より）:
- リアクションを取得するエンドポイントは存在しない → 参加表明は「参加」返信で判定する。
- PUT /rooms/{id}/members はメンバー全体の置き換え。追加時は必ず現在のメンバーとマージする。
- メンバーに追加できるのはコンタクト済みユーザー（または組織内ユーザー）のみ。
- GET /rooms/{id}/messages は force=1 で最新 100 件。処理済み判定は DB 側で行う。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)
BASE = "https://api.chatwork.com/v2"


class ChatworkError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Chatwork API error {status}: {body[:300]}")
        self.status = status


class ChatworkClient:
    def __init__(
        self,
        token: str,
        *,
        dry_run: bool = True,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 4,
        sleep=time.sleep,
    ):
        self.token = token
        self.dry_run = dry_run
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=BASE,
            headers={"x-chatworktoken": token, "accept": "application/json"},
            timeout=20.0,
            transport=transport,
        )
        self._fake_seq = 0

    # ------------------------------------------------------------ 低レベル
    def _request(self, method: str, path: str, **kw) -> Any:
        if not self.token:
            raise ChatworkError(0, "CHATWORK API トークンが未設定です")
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                r = self._http.request(method, path, **kw)
            except httpx.TransportError as e:
                if attempt >= self.max_retries:
                    raise ChatworkError(0, f"network error: {e}") from e
                self._sleep(delay)
                delay *= 2
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt >= self.max_retries:
                    raise ChatworkError(r.status_code, r.text)
                wait = delay
                reset = r.headers.get("x-ratelimit-reset")
                if r.status_code == 429 and reset and reset.isdigit():
                    wait = max(1.0, min(300.0, int(reset) - time.time()))
                log.warning("Chatwork %s %s -> %s, retry in %.0fs", method, path, r.status_code, wait)
                self._sleep(wait)
                delay *= 2
                continue
            if r.status_code == 204 or not r.content:
                return None
            if r.status_code >= 400:
                raise ChatworkError(r.status_code, r.text)
            return r.json()
        raise ChatworkError(0, "unreachable")

    def _fake(self, kind: str, **info) -> dict:
        self._fake_seq += 1
        log.info("[DRY_RUN] Chatwork %s %s", kind, info)
        return {"dry_run": True, "message_id": f"dryrun-{self._fake_seq}", **info}

    # ------------------------------------------------------------ 読み取り
    def me(self) -> dict:
        return self._request("GET", "/me")

    def rooms(self) -> list[dict]:
        return self._request("GET", "/rooms") or []

    def contacts(self) -> list[dict]:
        return self._request("GET", "/contacts") or []

    def incoming_requests(self) -> list[dict]:
        return self._request("GET", "/incoming_requests") or []

    def members(self, room_id: int) -> list[dict]:
        return self._request("GET", f"/rooms/{room_id}/members") or []

    def messages(self, room_id: int, force: bool = True) -> list[dict]:
        return self._request("GET", f"/rooms/{room_id}/messages", params={"force": 1 if force else 0}) or []

    def message(self, room_id: int, message_id: str) -> dict:
        return self._request("GET", f"/rooms/{room_id}/messages/{message_id}")

    # ------------------------------------------------------------ 書き込み
    def post_message(self, room_id: int, body: str) -> dict:
        if self.dry_run:
            return self._fake("post_message", room_id=room_id, chars=len(body))
        return self._request("POST", f"/rooms/{room_id}/messages", data={"body": body, "self_unread": 0})

    def approve_incoming_request(self, request_id: int) -> dict:
        if self.dry_run:
            return self._fake("approve_incoming_request", request_id=request_id)
        return self._request("PUT", f"/incoming_requests/{request_id}")

    def add_room_members(self, room_id: int, account_ids: list[int], role: str = "member") -> dict:
        """既存メンバーと権限を維持したまま追加する（PUT は全置換のため）。"""
        current = self.members(room_id)
        roles: dict[str, set[int]] = {"admin": set(), "member": set(), "readonly": set()}
        for m in current:
            roles.setdefault(m["role"], set()).add(int(m["account_id"]))
        existing = set().union(*roles.values())
        new_ids = [a for a in account_ids if a not in existing]
        if not new_ids:
            return {"added": [], "note": "既にメンバーです"}
        roles[role].update(new_ids)
        if not roles["admin"]:
            raise ChatworkError(0, "管理者が 0 人になるため中止しました")
        if self.dry_run:
            return self._fake("add_room_members", room_id=room_id, added=new_ids)
        data = {
            "members_admin_ids": ",".join(map(str, sorted(roles["admin"]))),
            "members_member_ids": ",".join(map(str, sorted(roles["member"]))),
            "members_readonly_ids": ",".join(map(str, sorted(roles["readonly"]))),
        }
        res = self._request("PUT", f"/rooms/{room_id}/members", data=data)
        return {"added": new_ids, "response": res}

    def upload_file(self, room_id: int, filename: str, content: bytes, message: str = "") -> dict:
        if self.dry_run:
            return self._fake("upload_file", room_id=room_id, filename=filename, size=len(content))
        if len(content) > 5 * 1024 * 1024:
            raise ChatworkError(0, "ファイルが 5MB を超えています")
        return self._request(
            "POST",
            f"/rooms/{room_id}/files",
            files={"file": (filename, content)},
            data={"message": message} if message else None,
        )

    # ------------------------------------------------------------ 補助
    def find_direct_room(self, account_id: int) -> int | None:
        """ダイレクトチャットの room_id を探す（/contacts に room_id が含まれる）。"""
        for c in self.contacts():
            if int(c.get("account_id", 0)) == int(account_id):
                return int(c["room_id"])
        return None


def reply_header(account_id: int, room_id: int, message_id: str, name: str) -> str:
    return f"[rp aid={account_id} to={room_id}-{message_id}]{name}さん\n"


def to_header(account_id: int, name: str) -> str:
    return f"[To:{account_id}]{name}さん\n"
