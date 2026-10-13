"""コマンドライン:

  python -m enpra.cli hash-password            管理者パスワードのハッシュを生成
  python -m enpra.cli verify                   API 実現可能性の実地検証（読み取りのみ）
  python -m enpra.cli run-job <name>           定期ジョブを手動実行（DRY_RUN 設定に従う）
  python -m enpra.cli list-jobs                ジョブ一覧
"""
from __future__ import annotations

import getpass
import json
import sys


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "hash-password":
        from .security import hash_password

        pw = getpass.getpass("管理者パスワード: ")
        if len(pw) < 12:
            print("12 文字以上にしてください", file=sys.stderr)
            return 1
        if pw != getpass.getpass("確認: "):
            print("一致しません", file=sys.stderr)
            return 1
        print(hash_password(pw))
        return 0

    from .config import Settings

    settings = Settings.from_env()
    if cmd == "verify":
        from .verify import as_markdown, run

        print(as_markdown(run(settings)))
        return 0
    if cmd == "list-jobs":
        from .jobs_runner import JOBS

        for k, (desc, _) in JOBS.items():
            print(f"{k:16s} {desc}")
        return 0
    if cmd == "run-job" and len(argv) == 2:
        from . import db
        from .app_factory import build_context
        from .jobs_runner import run_job

        db.init_db(settings.database_url)
        ctx = build_context(settings)
        print(f"DRY_RUN={settings.dry_run} NOTIFY_DRY_RUN={settings.notify_dry_run}")
        with db.session_scope() as s:
            print(json.dumps(run_job(s, ctx, argv[1]), default=str, ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
