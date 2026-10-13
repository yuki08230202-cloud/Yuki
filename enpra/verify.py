"""API 実現可能性の実地検証（読み取り専用。送信・変更は一切行わない）。

使い方:  python -m enpra.cli verify
本番トークンを設定した環境で実行し、結果を docs/feasibility.md の「実地検証」欄に転記する。
"""
from __future__ import annotations

from .chatwork import ChatworkClient, ChatworkError
from .config import Settings


def run(settings: Settings) -> list[tuple[str, str, str]]:
    """[(項目, 結果 OK/NG/WARN/SKIP, 詳細)]"""
    out: list[tuple[str, str, str]] = []
    cw = ChatworkClient(settings.chatwork_api_token, dry_run=True, max_retries=1)

    def check(name, fn):
        try:
            out.append((name, *fn()))
        except ChatworkError as e:
            out.append((name, "NG", str(e)))
        except Exception as e:  # noqa: BLE001
            out.append((name, "NG", f"{type(e).__name__}: {e}"))

    if not settings.chatwork_api_token:
        out.append(("Chatwork API トークン", "NG", "CHATWORK_API_TOKEN 未設定"))
    else:
        def me():
            m = cw.me()
            ok = m.get("chatwork_id") == settings.admin_chatwork_id
            return ("OK" if ok else "WARN",
                    f"account_id={m.get('account_id')} chatwork_id={m.get('chatwork_id')} name={m.get('name')}"
                    + ("" if ok else f"（設定の ADMIN_CHATWORK_ID={settings.admin_chatwork_id} と不一致）"))
        check("GET /me（トークン所有者・account_id の確認）", me)

        def rooms():
            rs = cw.rooms()
            my = [r for r in rs if r.get("type") == "my"]
            groups = [f"{r['room_id']}:{r['name']}" for r in rs if r.get("type") == "group"][:15]
            return "OK", f"ルーム {len(rs)} 件。マイチャット room_id={my[0]['room_id'] if my else '-'}。グループ例: {groups}"
        check("GET /rooms（通知用 room_id・募集ルームの特定）", rooms)

        def contacts():
            cs = cw.contacts()
            n = len(cs)
            st = "WARN" if n >= settings.contact_warn_at else "OK"
            return st, f"コンタクト {n}/{settings.contact_limit} 名（残り {max(0, settings.contact_limit - n)}）"
        check("GET /contacts（コンタクト数）", contacts)
        check("GET /incoming_requests（コンタクト申請の取得）",
              lambda: ("OK", f"申請 {len(cw.incoming_requests())} 件（承認は PUT /incoming_requests/{{id}}）"))

        if settings.recruit_room_id:
            def members():
                ms = cw.members(settings.recruit_room_id)
                admins = [m["name"] for m in ms if m["role"] == "admin"]
                return "OK", f"メンバー {len(ms)} 名、管理者: {admins}（招待は PUT /rooms/{{id}}/members・全置換）"
            check("GET /rooms/{募集ルーム}/members（招待権限の確認）", members)

            def msgs():
                ms = cw.messages(settings.recruit_room_id, force=True)
                keys = set().union(*(m.keys() for m in ms)) if ms else set()
                has_reaction = any("reaction" in k for k in keys)
                return ("OK" if has_reaction else "WARN",
                        f"直近 {len(ms)} 件取得。メッセージ項目: {sorted(keys)}。"
                        + ("リアクション項目あり" if has_reaction else "リアクション情報は API に含まれない → 「参加」返信方式を使用"))
            check("GET /rooms/{募集ルーム}/messages（リアクション取得可否）", msgs)
        else:
            out.append(("募集ルーム", "SKIP", "RECRUIT_ROOM_ID 未設定"))

    if settings.chatwork_notify_token:
        ncw = ChatworkClient(settings.chatwork_notify_token, dry_run=True, max_retries=1)
        check("通知 Bot トークン GET /me", lambda: ("OK", str({k: ncw.me().get(k) for k in ("account_id", "name")})))
    else:
        out.append(("通知 Bot トークン", "WARN",
                    "未設定。管理者本人のトークンで送ると自分の発言扱いになり、通知（バッジ・プッシュ）が鳴りません"))

    if settings.google_sheet_id:
        def sheets():
            from .sheets import SheetsClient

            sc = SheetsClient(settings.google_sheet_id, credentials_file=settings.google_service_account_file,
                              dry_run=True, record_tab=settings.sheet_record_tab)
            vals = sc.read(f"'{settings.sheet_record_tab}'!A1:G3")
            return "OK", f"「{settings.sheet_record_tab}」読取 OK。先頭行: {vals[:1]}"
        check("Google Sheets 読み取り", sheets)
    else:
        out.append(("Google Sheets", "SKIP", "GOOGLE_SHEET_ID 未設定"))

    out.append(("Indeed 応募者への送信 API", "NG",
                "公開 API は ATS パートナー向けで事前書面承認が必要。雇用主→応募者メッセージ API は確認できず。"
                "CSV 取込＋承認後の手動送信で運用（docs/feasibility.md）"))
    return out


def as_markdown(rows: list[tuple[str, str, str]]) -> str:
    lines = ["| 項目 | 結果 | 詳細 |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {c.replace('|', '/')} |" for a, b, c in rows]
    return "\n".join(lines)
