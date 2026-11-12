"""Scheduling for unattended cron / systemd operation.

Deliberately not a daemon and not a framework: cron (or a systemd timer) calls
``python -m outreach scheduler tick`` and this module decides what, if
anything, is due.  Overlapping invocations are prevented by a database lock,
so a slow run can never be stacked on top of itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach.config import Settings
from outreach.db import utcnow
from outreach.logging import get_logger
from outreach.messaging.compliance import in_send_window, next_window_open, sends_today
from outreach.messaging.queue import pending_messages
from outreach.models.enums import MessageStatus
from outreach.models.outreach_message import OutreachMessage
from outreach.state import get_pause_state

log = get_logger(__name__)

QUEUE_LOCK = "process-queue"
PIPELINE_LOCK = "pipeline-run"
REPLY_LOCK = "sync-replies"


@dataclass
class ScheduleDecision:
    """What the scheduler thinks should happen right now."""

    should_process_queue: bool = False
    should_sync_replies: bool = False
    reasons: list[str] = field(default_factory=list)
    next_send_window: datetime | None = None
    pending: int = 0
    sends_today: int = 0
    daily_limit: int = 0
    enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduler_enabled": self.enabled,
            "should_process_queue": self.should_process_queue,
            "should_sync_replies": self.should_sync_replies,
            "reasons": self.reasons,
            "next_send_window": self.next_send_window.isoformat() if self.next_send_window else None,
            "pending": self.pending,
            "sends_today": self.sends_today,
            "daily_limit": self.daily_limit,
        }


def evaluate_schedule(
    session: Session, settings: Settings, now: datetime | None = None
) -> ScheduleDecision:
    """Decide what is due, without doing any of it."""
    now = now or utcnow()
    decision = ScheduleDecision(enabled=settings.features.scheduler)
    decision.pending = len(pending_messages(session, limit=500))
    decision.sends_today = sends_today(session, settings, now)
    decision.daily_limit = settings.effective_daily_send_limit
    decision.next_send_window = next_window_open(settings, now)

    if not settings.features.scheduler:
        decision.reasons.append("SKIPPED_FEATURE_DISABLED: FEATURE_SCHEDULER is off")
        return decision

    pause = get_pause_state(session)
    if pause.paused:
        decision.reasons.append(f"system is paused: {pause.summary}")
        return decision

    inside, why = in_send_window(settings, now)
    if not decision.pending:
        decision.reasons.append("nothing is queued")
    elif not inside:
        decision.reasons.append(why)
    elif decision.sends_today >= decision.daily_limit:
        decision.reasons.append(
            f"daily send limit reached ({decision.sends_today}/{decision.daily_limit})"
        )
    else:
        decision.should_process_queue = True
        decision.reasons.append(
            f"{decision.pending} message(s) queued, {decision.sends_today}/"
            f"{decision.daily_limit} sent today"
        )

    if settings.features.reply_sync:
        decision.should_sync_replies = True
        decision.reasons.append("reply sync is enabled")
    else:
        decision.reasons.append("SKIPPED_FEATURE_DISABLED: FEATURE_REPLY_SYNC is off")

    return decision


def due_scheduled_messages(
    session: Session, now: datetime | None = None, limit: int = 50
) -> list[OutreachMessage]:
    """Scheduled messages whose time has arrived."""
    now = now or utcnow()
    return list(
        session.scalars(
            select(OutreachMessage)
            .where(
                OutreachMessage.status == MessageStatus.SCHEDULED,
                OutreachMessage.scheduled_at.is_not(None),
                OutreachMessage.scheduled_at <= now,
            )
            .order_by(OutreachMessage.scheduled_at)
            .limit(limit)
        ).all()
    )


def plan_send_times(
    settings: Settings, count: int, start: datetime | None = None
) -> list[datetime]:
    """Evenly spaced slots inside the send window.

    Regular intervals on purpose: we are not trying to look human to a spam
    filter, we are trying to be a reasonable neighbour to a mail server.
    """
    slots: list[datetime] = []
    cursor = start or utcnow()
    gap = timedelta(seconds=settings.effective_min_seconds_between_sends)
    per_day = settings.effective_daily_send_limit
    if per_day <= 0 or count <= 0:
        return slots
    today_count = 0
    for _ in range(count):
        inside, _ = in_send_window(settings, cursor)
        if today_count >= per_day:
            cursor = next_window_open(settings, cursor, force_next_day=True)
            today_count = 0
        elif not inside:
            cursor = next_window_open(settings, cursor)
            today_count = 0
        slots.append(cursor)
        today_count += 1
        cursor = cursor + gap
    return slots


CRON_EXAMPLES = """\
# --- StoopCraft outreach: crontab examples -------------------------------
# Every run is bounded, idempotent and guarded by a database lock, so an
# overlapping invocation exits immediately instead of doubling up.
#
# Discover + scan + qualify, once each weekday morning:
0 8 * * 1-5  cd /srv/stoopcraft-outreach && .venv/bin/python -m outreach run \\
               --vertical dentist --city Phoenix --state AZ >> logs/run.log 2>&1
#
# Work the send queue every 20 minutes during the day (the send window,
# daily limit and minimum interval are all enforced inside the app):
*/20 9-17 * * 1-5  cd /srv/stoopcraft-outreach && .venv/bin/python -m outreach \\
               process-queue >> logs/queue.log 2>&1
#
# Poll for replies and opt-outs hourly:
15 * * * *   cd /srv/stoopcraft-outreach && .venv/bin/python -m outreach \\
               sync-replies >> logs/replies.log 2>&1
#
# Daily report at 18:00:
0 18 * * *   cd /srv/stoopcraft-outreach && .venv/bin/python -m outreach today \\
               >> logs/today.log 2>&1
"""

SYSTEMD_EXAMPLES = """\
# --- /etc/systemd/system/stoopcraft-queue.service ------------------------
[Unit]
Description=StoopCraft outreach send queue
After=network-online.target

[Service]
Type=oneshot
User=stoopcraft
WorkingDirectory=/srv/stoopcraft-outreach
EnvironmentFile=/srv/stoopcraft-outreach/.env
ExecStart=/srv/stoopcraft-outreach/.venv/bin/python -m outreach process-queue
# The app already sandboxes the AI subprocess; these harden the unit itself.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/srv/stoopcraft-outreach/data /srv/stoopcraft-outreach/artifacts \\
               /srv/stoopcraft-outreach/previews /srv/stoopcraft-outreach/logs

# --- /etc/systemd/system/stoopcraft-queue.timer --------------------------
[Unit]
Description=Run the StoopCraft send queue during business hours

[Timer]
OnCalendar=Mon..Fri 09:00..16:30/00:20
Persistent=true

[Install]
WantedBy=timers.target

# --- /etc/systemd/system/stoopcraft-pipeline.service ---------------------
# Same shape, with:
# ExecStart=/srv/stoopcraft-outreach/.venv/bin/python -m outreach run \\
#           --vertical dentist --city Phoenix --state AZ
# and a timer of OnCalendar=Mon..Fri 08:00
#
# Enable with:
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now stoopcraft-queue.timer
#
# This project never edits the host's cron or systemd configuration itself.
"""
