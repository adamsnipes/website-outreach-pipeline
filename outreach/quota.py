"""Metered-provider quota accounting and enforcement.

The rule is absolute: a metered provider request is only ever issued after
:func:`check_and_reserve` has returned an ``ALLOWED`` decision, and every
decision -- allowed or blocked -- is persisted.  Reaching a ceiling refuses the
request; it never merely logs a warning and it never silently falls back to a
different metered provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from outreach.db import utcnow
from outreach.events import record_event
from outreach.logging import get_logger
from outreach.models.enums import EventType, ProviderUsageStatus
from outreach.models.provider_usage import ProviderUsage

log = get_logger(__name__)

GOOGLE_PLACES = "google_places"


class QuotaExceeded(RuntimeError):
    """Raised when a metered request would exceed the local monthly ceiling."""


class ProviderBlocked(RuntimeError):
    """Raised when a metered request is refused for a non-quota reason."""


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    status: ProviderUsageStatus
    used: int
    limit: int
    reason: str | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def month_key(moment: datetime | None = None) -> str:
    return (moment or utcnow()).strftime("%Y-%m")


def monthly_usage(
    session: Session,
    provider: str,
    key: str | None = None,
    *,
    only_allowed: bool = True,
) -> int:
    """Billable units consumed this month for ``provider``."""
    stmt = select(func.coalesce(func.sum(ProviderUsage.billable_units), 0)).where(
        ProviderUsage.provider == provider,
        ProviderUsage.month_key == (key or month_key()),
    )
    if only_allowed:
        stmt = stmt.where(ProviderUsage.status == ProviderUsageStatus.ALLOWED)
    return int(session.scalar(stmt) or 0)


def record_usage(
    session: Session,
    provider: str,
    operation: str,
    status: ProviderUsageStatus,
    *,
    billable_units: int = 1,
    result_count: int | None = None,
    campaign_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
) -> ProviderUsage:
    """Persist one provider request attempt (blocked attempts included)."""
    usage = ProviderUsage(
        provider=provider,
        operation=operation,
        month_key=month_key(),
        status=status,
        billable_units=billable_units if status == ProviderUsageStatus.ALLOWED else 0,
        result_count=result_count,
        campaign_id=campaign_id,
        metadata_json=metadata or None,
        error=error,
    )
    session.add(usage)
    session.flush()
    if status != ProviderUsageStatus.ALLOWED:
        record_event(
            session,
            "provider",
            None,
            EventType.PROVIDER_BLOCKED,
            {
                "provider": provider,
                "operation": operation,
                "status": str(status),
                "error": error,
            },
        )
    return usage


def check_and_reserve(
    session: Session,
    provider: str,
    operation: str,
    *,
    limit: int,
    feature_enabled: bool,
    has_credential: bool,
    paused: bool = False,
    units: int = 1,
    campaign_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> QuotaDecision:
    """Authorise a single metered request and reserve its quota units.

    The reservation row is written *before* the network call, so a crash mid
    request can only ever over-count, never under-count.
    """
    used = monthly_usage(session, provider)

    def blocked(status: ProviderUsageStatus, reason: str) -> QuotaDecision:
        record_usage(
            session,
            provider,
            operation,
            status,
            billable_units=0,
            campaign_id=campaign_id,
            metadata=metadata,
            error=reason,
        )
        log.error(
            "metered provider request refused",
            extra={
                "provider": provider,
                "operation": operation,
                "status": str(status),
                "reason": reason,
                "used": used,
                "limit": limit,
            },
        )
        return QuotaDecision(False, status, used, limit, reason)

    if not feature_enabled:
        return blocked(
            ProviderUsageStatus.BLOCKED_FEATURE_DISABLED,
            "FEATURE_REAL_DISCOVERY is disabled",
        )
    if paused:
        return blocked(ProviderUsageStatus.BLOCKED_PAUSED, "System is globally paused")
    if not has_credential:
        return blocked(
            ProviderUsageStatus.BLOCKED_NO_CREDENTIAL, f"No credential configured for {provider}"
        )
    if limit <= 0:
        return blocked(ProviderUsageStatus.BLOCKED_QUOTA, "Monthly request limit is 0")
    if used + units > limit:
        return blocked(
            ProviderUsageStatus.BLOCKED_QUOTA,
            f"Monthly ceiling reached: {used}/{limit} units already used",
        )

    record_usage(
        session,
        provider,
        operation,
        ProviderUsageStatus.ALLOWED,
        billable_units=units,
        campaign_id=campaign_id,
        metadata=metadata,
    )
    return QuotaDecision(True, ProviderUsageStatus.ALLOWED, used + units, limit)


def usage_report(session: Session, key: str | None = None) -> list[dict[str, Any]]:
    """Per-provider monthly accounting including blocked attempts."""
    key = key or month_key()
    rows = session.execute(
        select(
            ProviderUsage.provider,
            ProviderUsage.status,
            func.count(ProviderUsage.id),
            func.coalesce(func.sum(ProviderUsage.billable_units), 0),
        )
        .where(ProviderUsage.month_key == key)
        .group_by(ProviderUsage.provider, ProviderUsage.status)
    ).all()
    grouped: dict[str, dict[str, Any]] = {}
    for provider, status, count, units in rows:
        entry = grouped.setdefault(
            provider,
            {"provider": provider, "month": key, "billable_units": 0, "by_status": {}},
        )
        entry["by_status"][str(status)] = int(count)
        entry["billable_units"] += int(units)
    return sorted(grouped.values(), key=lambda e: e["provider"])
