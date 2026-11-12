"""String enums used across the schema.

Stored as plain strings so the SQLite file remains readable with any tool and
so adding a value never requires a migration.
"""

from __future__ import annotations

from enum import StrEnum


class CampaignStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class ScanStatus(StrEnum):
    PENDING = "PENDING"
    OK = "OK"
    HTTP_ERROR = "HTTP_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"
    BLOCKED = "BLOCKED"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
    NO_WEBSITE = "NO_WEBSITE"
    RENDER_ERROR = "RENDER_ERROR"
    FAILED = "FAILED"


class QualificationStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    GOOD_SITE = "GOOD_SITE"
    BORDERLINE = "BORDERLINE"
    REDESIGN_CANDIDATE = "REDESIGN_CANDIDATE"
    STRONG_REDESIGN_CANDIDATE = "STRONG_REDESIGN_CANDIDATE"
    UNREACHABLE = "UNREACHABLE"


class ContactabilityStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    CONTACTABLE = "CONTACTABLE"
    NO_PUBLIC_EMAIL = "NO_PUBLIC_EMAIL"
    SUPPRESSED = "SUPPRESSED"


class ContactType(StrEnum):
    OWNER = "owner"
    DOCTOR = "doctor"
    OFFICE = "office"
    HELLO = "hello"
    CONTACT = "contact"
    INFO = "info"
    SUPPORT = "support"
    UNKNOWN = "unknown"


class BuildStatus(StrEnum):
    PENDING = "PENDING"
    BUILDING = "BUILDING"
    BUILT = "BUILT"
    QA_FAILED = "QA_FAILED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class MessageStatus(StrEnum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    SENDING = "SENDING"
    SENT = "SENT"
    REPLIED = "REPLIED"
    POSITIVE_REPLY = "POSITIVE_REPLY"
    NEGATIVE_REPLY = "NEGATIVE_REPLY"
    OPTED_OUT = "OPTED_OUT"
    BOUNCED = "BOUNCED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


#: Statuses that prove an outreach attempt already left the building.  Used to
#: guarantee "exactly one initial email per business".
TERMINAL_SENT_STATUSES = frozenset(
    {
        MessageStatus.SENDING,
        MessageStatus.SENT,
        MessageStatus.REPLIED,
        MessageStatus.POSITIVE_REPLY,
        MessageStatus.NEGATIVE_REPLY,
        MessageStatus.OPTED_OUT,
        MessageStatus.BOUNCED,
        MessageStatus.NEEDS_REVIEW,
    }
)


class SuppressionReason(StrEnum):
    REQUESTED = "requested"
    REPLY_OPT_OUT = "reply_opt_out"
    BOUNCE = "bounce"
    COMPLAINT = "complaint"
    MANUAL = "manual"
    INVALID = "invalid"


class ProviderUsageStatus(StrEnum):
    ALLOWED = "ALLOWED"
    BLOCKED_FEATURE_DISABLED = "BLOCKED_FEATURE_DISABLED"
    BLOCKED_NO_CREDENTIAL = "BLOCKED_NO_CREDENTIAL"
    BLOCKED_QUOTA = "BLOCKED_QUOTA"
    BLOCKED_PAUSED = "BLOCKED_PAUSED"
    ERROR = "ERROR"


class AIPurpose(StrEnum):
    HEALTH_CHECK = "health_check"
    SCORE_WEBSITE = "score_website"
    EXTRACT_BUSINESS_CONTENT = "extract_business_content"
    GENERATE_SITE_CONTENT = "generate_site_content"
    COMPOSE_EMAIL = "compose_email"
    CLASSIFY_REPLY = "classify_reply"


class EventType(StrEnum):
    CAMPAIGN_CREATED = "campaign_created"
    BUSINESS_DISCOVERED = "business_discovered"
    BUSINESS_REDISCOVERED = "business_rediscovered"
    SCAN_STARTED = "scan_started"
    SCAN_COMPLETED = "scan_completed"
    SCAN_FAILED = "scan_failed"
    CONTACT_FOUND = "contact_found"
    AI_SCORED = "ai_scored"
    QUALIFIED = "qualified"
    CONTENT_EXTRACTED = "content_extracted"
    BUILD_STARTED = "build_started"
    BUILD_COMPLETED = "build_completed"
    BUILD_FAILED = "build_failed"
    QA_PASSED = "qa_passed"
    QA_FAILED = "qa_failed"
    PREVIEW_PUBLISHED = "preview_published"
    MESSAGE_COMPOSED = "message_composed"
    MESSAGE_QUEUED = "message_queued"
    MESSAGE_SCHEDULED = "message_scheduled"
    MESSAGE_SENT = "message_sent"
    MESSAGE_FAILED = "message_failed"
    MESSAGE_SKIPPED = "message_skipped"
    MESSAGE_NEEDS_REVIEW = "message_needs_review"
    REPLY_RECEIVED = "reply_received"
    OPT_OUT = "opt_out"
    SUPPRESSED = "suppressed"
    SUPPRESSION_REMOVED = "suppression_removed"
    SYSTEM_PAUSED = "system_paused"
    SYSTEM_RESUMED = "system_resumed"
    STAGE_SKIPPED_FEATURE_DISABLED = "SKIPPED_FEATURE_DISABLED"
    PROVIDER_BLOCKED = "provider_blocked"
    PIPELINE_STARTED = "pipeline_started"
    PIPELINE_FINISHED = "pipeline_finished"
