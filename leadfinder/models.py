from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
EXPORT_DIR = BASE_DIR / "exports"
PROFILE_DIR = BASE_DIR / ".maps-profile"
DEFAULT_LIMIT = 100
DEFAULT_PAUSE = 1.5
MAX_LIMIT = 120
PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

SOCIAL_DOMAINS = ("facebook.com", "fb.com", "instagram.com", "youtube.com", "youtu.be")
MESSAGEABLE_SOCIAL_DOMAINS = ("facebook.com", "fb.com", "instagram.com")
DIRECTORY_DOMAINS = (
    "justdial.com",
    "sulekha.com",
    "indiamart.com",
    "tradeindia.com",
    "yellowpages.com",
    "yellowpages.in",
)


class DependencyError(RuntimeError):
    """Raised when an optional runtime dependency is missing."""


@dataclass
class Lead:
    priority: str
    priority_reason: str
    business_name: str
    contact: str = ""
    all_contacts: str = ""
    contact_source: str = ""
    address: str = ""
    timing: str = ""
    website: str = ""
    website_source: str = ""
    website_status: str = ""
    website_type: str = ""
    website_http_status: str = ""
    social_profiles: str = ""
    directory_profiles: str = ""
    message_profiles: str = ""
    category: str = ""
    rating: str = ""
    review_count: str = ""
    maps_url: str = ""


@dataclass
class WebsiteDecision:
    keep: bool
    priority: str
    priority_reason: str
    website_status: str
    website_type: str
    website_http_status: str = ""
    website: str = ""


@dataclass(frozen=True)
class ContactCandidate:
    value: str
    kind: str
    source: str
    rank: int


@dataclass(frozen=True)
class WebsiteCandidate:
    url: str
    source: str
    rank: int


@dataclass
class PageSnapshot:
    url: str
    final_url: str
    title: str = ""
    text: str = ""
    html: str = ""
    links: list[str] = field(default_factory=list)

