"""Google Sheets 連携。

「企業フォーム送付_記録」タブの列構成（1 行目は見出し）:
  A:日付(YYYY-MM-DD)  B:ワーカー名  C:account_id  D:送信完了  E:送信不可  F:アポ数  G:更新日時
列構成が既存シートと異なる場合は RECORD_COLUMNS を合わせて変更する。

更新前後の値は SheetChange に保存し、restore() で復元できる。
DRY_RUN 中は書き込まず、予定の差分だけを記録する。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import clock
from .models import SheetChange

RECORD_COLUMNS = ["日付", "ワーカー名", "account_id", "送信完了", "送信不可", "アポ数", "更新日時"]


class SheetsClient:
    def __init__(self, sheet_id: str, *, service=None, credentials_file: str = "", dry_run: bool = True,
                 record_tab: str = "企業フォーム送付_記録", worklog_tab: str = ""):
        self.sheet_id = sheet_id
        self.dry_run = dry_run
        self.record_tab = record_tab
        self.worklog_tab = worklog_tab
        if service is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            if credentials_file:
                creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=scopes)
            else:
                import google.auth

                creds, _ = google.auth.default(scopes=scopes)
            service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._values = service.spreadsheets().values()

    # ------------------------------------------------------------ 基本操作
    def read(self, rng: str) -> list[list]:
        res = self._values.get(spreadsheetId=self.sheet_id, range=rng).execute()
        return res.get("values", [])

    def _write(self, rng: str, rows: list[list]) -> None:
        self._values.update(spreadsheetId=self.sheet_id, range=rng, valueInputOption="USER_ENTERED",
                            body={"values": rows}).execute()

    def write_with_backup(self, db: Session, rng: str, rows: list[list], approval_id: int | None) -> SheetChange:
        before = self.read(rng)
        ch = SheetChange(approval_id=approval_id, sheet_range=rng, before=before, after=rows,
                         status="dry_run" if self.dry_run else "applied")
        if not self.dry_run:
            self._write(rng, rows)
        db.add(ch)
        db.flush()
        return ch

    def restore(self, db: Session, change: SheetChange) -> SheetChange:
        if change.status != "applied":
            raise ValueError("適用済みの変更のみ復元できます")
        width = max([len(r) for r in change.after] + [len(r) for r in change.before] + [1])
        height = max(len(change.after), len(change.before))
        rows = [list(r) + [""] * (width - len(r)) for r in change.before]
        rows += [[""] * width for _ in range(height - len(rows))]
        if not self.dry_run:
            self._write(change.sheet_range, rows)
        change.status = "restored"
        db.flush()
        return change

    # ------------------------------------------------------------ 業務用
    def upsert_daily_records(self, db: Session, work_date: str, records: list[dict], approval_id: int | None) -> list[SheetChange]:
        tab = self.record_tab
        values = self.read(f"'{tab}'!A:G")
        if not values:
            self.write_with_backup(db, f"'{tab}'!A1:G1", [RECORD_COLUMNS], approval_id)
            values = [RECORD_COLUMNS]
        index = {(r[0], str(r[2])): i + 1 for i, r in enumerate(values) if len(r) >= 3}
        now = clock.now_jst().strftime("%Y-%m-%d %H:%M")
        changes = []
        next_row = len(values) + 1
        for rec in records:
            row = [work_date, rec["name"], str(rec["account_id"]), rec["sent"], rec["unsendable"],
                   rec.get("appointments", 0), now]
            rownum = index.get((work_date, str(rec["account_id"])))
            if rownum is None:
                rownum = next_row
                next_row += 1
            changes.append(self.write_with_backup(db, f"'{tab}'!A{rownum}:G{rownum}", [row], approval_id))
        return changes

    def worklog_counts(self, work_date: str) -> dict[str, dict] | None:
        """照合元タブ（日付, ワーカー名, ステータス[送信完了/送信不可]）から件数を数える。"""
        if not self.worklog_tab:
            return None
        counts: dict[str, dict] = {}
        for r in self.read(f"'{self.worklog_tab}'!A:C")[1:]:
            if len(r) < 3 or r[0] != work_date:
                continue
            c = counts.setdefault(r[1], {"sent": 0, "unsendable": 0})
            if "不可" in r[2]:
                c["unsendable"] += 1
            elif "完了" in r[2] or "送信" in r[2]:
                c["sent"] += 1
        return counts

    def monthly_totals(self, month: str) -> dict[str, dict]:
        """記録タブから月次の送信件数・アポ数を account_id 単位で集計する。"""
        totals: dict[str, dict] = {}
        for r in self.read(f"'{self.record_tab}'!A:F")[1:]:
            if len(r) < 4 or not str(r[0]).startswith(month):
                continue
            t = totals.setdefault(str(r[2]), {"name": r[1], "sent": 0, "appointments": 0})
            t["sent"] += _int(r[3])
            t["appointments"] += _int(r[5]) if len(r) > 5 else 0
        return totals

    def available_list_count(self) -> int | None:
        return None  # 営業リストのシート構成が決まり次第実装


def _int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0
