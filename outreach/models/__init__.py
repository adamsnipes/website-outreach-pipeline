"""Every mapper is imported here so Alembic autogenerate sees the full schema."""

from outreach.models.ai_assessment import AIWebsiteAssessment
from outreach.models.ai_usage import AIUsage
from outreach.models.build import Build
from outreach.models.business import Business, CampaignBusiness
from outreach.models.business_content import BusinessContent
from outreach.models.campaign import Campaign
from outreach.models.contact import Contact
from outreach.models.enums import (
    AIPurpose,
    BuildStatus,
    CampaignStatus,
    ContactabilityStatus,
    ContactType,
    EventType,
    MessageStatus,
    ProviderUsageStatus,
    QualificationStatus,
    ScanStatus,
    SuppressionReason,
)
from outreach.models.event import Event
from outreach.models.outreach_message import OutreachMessage
from outreach.models.provider_usage import ProviderUsage
from outreach.models.scan import WebsiteScan
from outreach.models.suppression import Suppression
from outreach.models.system import PAUSE_KEY, JobLock, SystemState

__all__ = [
    "PAUSE_KEY",
    "AIPurpose",
    "AIUsage",
    "AIWebsiteAssessment",
    "Build",
    "BuildStatus",
    "Business",
    "BusinessContent",
    "Campaign",
    "CampaignBusiness",
    "CampaignStatus",
    "Contact",
    "ContactType",
    "ContactabilityStatus",
    "Event",
    "EventType",
    "JobLock",
    "MessageStatus",
    "OutreachMessage",
    "ProviderUsage",
    "ProviderUsageStatus",
    "QualificationStatus",
    "ScanStatus",
    "Suppression",
    "SuppressionReason",
    "SystemState",
    "WebsiteScan",
]
