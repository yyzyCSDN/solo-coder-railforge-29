from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ReleaseEvent:
    """A customs release (or revocation) authorization for a marshalled subject."""
    event_id: str
    subject: str
    scope: str
    kind: str            # 'release' grants clearance, 'revoke' withdraws it
    at: datetime


def released(rows: list[ReleaseEvent], subject: str, scope: str, at: datetime):
    """Return the latest in-scope release authorization in force at ``at``.

    Returns the governing :class:`ReleaseEvent` when the subject currently holds
    an unrevoked customs release for the scope, otherwise ``None``. Events after
    ``at`` are ignored and revocations cancel any prior release.
    """
    x = at.astimezone(timezone.utc)
    current = None
    for e in sorted(rows, key=lambda r: r.at.astimezone(timezone.utc)):
        if e.subject != subject or e.scope != scope:
            continue
        if e.at.astimezone(timezone.utc) > x:
            continue
        if e.kind == 'release':
            current = e
        elif e.kind == 'revoke':
            current = None
    return current
