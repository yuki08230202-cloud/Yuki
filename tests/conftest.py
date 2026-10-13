from __future__ import annotations

from datetime import datetime

import pytest

from enpra import clock, db as dbm
from enpra.config import Settings
from enpra.context import AppContext
from enpra.jobs import *  # noqa: F401,F403
from enpra.notifier import Notifier

ADMIN = 1000
RECRUIT_ROOM = 500


class FakeCW:
    """ChatworkClient と同じインターフェースのテスト用フェイク。"""

    def __init__(self):
        self.dry_run = False
        self.posts: list[tuple[int, str]] = []
        self.room_messages: dict[int, list[dict]] = {}
        self.contact_list: list[dict] = []
        self.requests: list[dict] = []
        self.approved: list[int] = []
        self.added: list[tuple[int, list[int]]] = []
        self.uploads: list[tuple[int, str]] = []
        self.fail_post_to: set[int] = set()
        self._seq = 0

    def post_message(self, room_id, body):
        if room_id in self.fail_post_to:
            raise RuntimeError("post failed")
        self._seq += 1
        self.posts.append((room_id, body))
        return {"message_id": str(9000 + self._seq)}

    def messages(self, room_id, force=True):
        return list(self.room_messages.get(room_id, []))

    def message(self, room_id, mid):
        return next(m for m in self.room_messages.get(room_id, []) if str(m["message_id"]) == str(mid))

    def contacts(self):
        return list(self.contact_list)

    def incoming_requests(self):
        return list(self.requests)

    def approve_incoming_request(self, rid):
        self.approved.append(rid)
        return {"account_id": 1, "room_id": 777}

    def add_room_members(self, room_id, ids, role="member"):
        self.added.append((room_id, ids))
        return {"added": ids}

    def upload_file(self, room_id, filename, content, message=""):
        self.uploads.append((room_id, filename))
        return {"file_id": 1}


class FakeNotifier(Notifier):
    def __init__(self, settings):
        super().__init__(settings, None)
        self.sent: list[tuple[str, str, str]] = []

    def notify(self, db, kind, title, body):
        self.sent.append((kind, title, body))
        return super().notify(db, kind, title, body)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        dry_run=False,
        notify_dry_run=True,
        base_url="https://enpra.example",
        database_url=f"sqlite:///{tmp_path}/t.db",
        admin_account_id=ADMIN,
        recruit_room_id=RECRUIT_ROOM,
        question_room_ids=[RECRUIT_ROOM],
        invoice_room_id=600,
        job_secret="jobsecret",
        session_secret="s" * 32,
    )


@pytest.fixture
def ctx(settings):
    dbm.init_db(settings.database_url)
    return AppContext(settings=settings, cw=FakeCW(), notifier=FakeNotifier(settings))


@pytest.fixture
def db(ctx):
    from enpra import approvals

    with dbm.session_scope() as s:
        approvals.ensure_job_settings(s)
        yield s


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.set_now(None)


def at(s: str) -> None:
    """'2026-10-13 18:30' (JST) に時刻を固定。"""
    clock.set_now(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=clock.JST))


def msg(mid, aid, name, body, when: str):
    t = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=clock.JST).timestamp()
    return {"message_id": str(mid), "account": {"account_id": aid, "name": name}, "body": body, "send_time": int(t)}
