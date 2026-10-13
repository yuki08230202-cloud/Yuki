"""外部送信の冪等化。同じ idempotency_key の送信は二度行わない。"""
from __future__ import annotations

from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import OutboundMessage


def send_once(
    db: Session,
    *,
    key: str,
    channel: str,
    target: str,
    body: str,
    approval_id: int | None,
    dry_run: bool,
    send: Callable[[], dict | None],
) -> OutboundMessage:
    existing = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == key))
    if existing is not None and existing.status in ("sent", "manual_pending"):
        return existing
    if existing is not None and existing.status == "dry_run" and dry_run:
        return existing
    om = existing or OutboundMessage(idempotency_key=key, channel=channel, target=target, body=body)
    om.approval_id = approval_id
    om.body = body
    try:
        res = send() or {}
        om.status = "dry_run" if (dry_run or res.get("dry_run")) else "sent"
        mid = res.get("message_id") or res.get("file_id")
        om.external_id = str(mid) if mid is not None else None
        om.error = None
    except Exception as e:
        om.status = "failed"
        om.error = str(e)[:1000]
        db.add(om)
        db.flush()
        raise
    db.add(om)
    db.flush()
    return om
