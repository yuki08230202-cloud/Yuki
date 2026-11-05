"""Chatwork のコンタクト申請をまとめて処理する（承認 → ワーカーグループ招待 → 資料送付）。

使い方:
    python scripts/onboard_contacts.py            # 実行
    python scripts/onboard_contacts.py --dry-run  # 確認のみ（書き込みなし）

環境変数 CHATWORK_API_TOKEN（管理者アカウント）が必要。
グループは RECRUIT_ROOM_ID、未設定なら「【ビジョン図鑑】フォーム送付グループ」。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from enpra.chatwork import ChatworkClient  # noqa: E402

DEFAULT_GROUP = 437764696  # 【ビジョン図鑑】フォーム送付グループ
WELCOME = (ROOT / "enpra" / "messages" / "welcome.txt").read_text(encoding="utf-8").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("CHATWORK_API_TOKEN", "")
    if not token:
        print("CHATWORK_API_TOKEN が未設定です", file=sys.stderr)
        return 2
    group = int(os.environ.get("RECRUIT_ROOM_ID") or DEFAULT_GROUP)
    cw = ChatworkClient(token, dry_run=args.dry_run)

    reqs = cw.incoming_requests()
    if not reqs:
        print("コンタクト申請はありません")
        return 0

    done = []
    for r in reqs:
        name = (r.get("name") or "").strip()
        aid = int(r["account_id"])
        print(f"- {name}（{r.get('chatwork_id')} / account {aid}）申請メッセージ: {r.get('message') or '(なし)'}")
        res = cw.approve_incoming_request(int(r["request_id"]))
        room_id = (res or {}).get("room_id")
        print(f"  承認: {'DRY_RUN' if args.dry_run else 'OK'} direct room={room_id}")
        done.append((name, aid, room_id))

    added = cw.add_room_members(group, [aid for _, aid, _ in done])
    print(f"グループ招待（room {group}）: {added.get('added')} {added.get('note', '')}")

    for name, aid, room_id in done:
        if not room_id and not args.dry_run:
            room_id = cw.find_direct_room(aid)
        if not room_id:
            print(f"  {name}: ダイレクトチャットが見つからず資料未送付", file=sys.stderr)
            continue
        cw.post_message(int(room_id), WELCOME.replace("{name}", name))
        print(f"  {name}: 資料送付 {'DRY_RUN' if args.dry_run else 'OK'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
