"""
Data models for job postings and evaluation results tailored for Mattia Carone's discovery layer.
"""

from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field, model_validator


class JobPosting(BaseModel):
    id: str = Field(description="Unique formatted ID: {source}:{slug}:{postingId} or li:{id}")
    title: str
    company: str
    location: Optional[str] = None
    location_source: Optional[str] = None
    location_scope: Optional[str] = None  # 'munich', 'remote-de', 'remote-eu' (Brief 8)
    url: str
    description: str = ""
    source: str = "general"
    posted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    first_seen: Optional[datetime] = None

    @model_validator(mode="after")
    def _normalize_location(self) -> "JobPosting":
        if self.location is not None and not str(self.location).strip():
            self.location = None
        if self.location is None and not self.location_source:
            self.location_source = "unknown"
        elif self.location is not None and not self.location_source:
            self.location_source = "source"
        if self.gate_reason and not self.rejection_reason:
            self.rejection_reason = self.gate_reason
        elif self.rejection_reason and not self.gate_reason:
            self.gate_reason = self.rejection_reason
        return self

    # Brief 2 tracking fields
    dedupe_key: Optional[str] = None
    company_display: Optional[str] = None
    apply_url: Optional[str] = None
    apply_url_source: Optional[str] = None  # 'firecrawl', 'source', 'resolved' (Brief 10)
    apply_url_status: Optional[str] = None  # 'resolved', 'none', 'blocked' (Brief 10)
    ats: Optional[str] = None
    ats_slug: Optional[str] = None
    closed_at: Optional[datetime] = None
    description_source: Optional[str] = None  # 'firecrawl', 'source' (Brief 10)

    # Evaluation & filtering results
    score: float = 0.0
    matched_skills: list[str] = Field(default_factory=list)
    matched_role: Optional[str] = None
    matched_categories: dict[str, list[str]] = Field(default_factory=dict)
    language_status: str = "English Friendly"
    fit_summary: str = ""

    # Gate tracking (for funnel reporting)
    gate_rejected: Optional[str] = None
    rejection_reason: Optional[str] = None
    gate_reason: Optional[str] = None

    # Application classification & host tracking (Brief 5, 7 & 9)
    apply_kind: str = "unknown"  # "auto", "captcha", "account", "easy_apply", "join-account", "unknown"
    apply_host: Optional[str] = None
    apply_route: Optional[str] = None  # greenhouse, ashby, lever, personio, smartrecruiters, teamtailor, recruitee, workable, linkedin_easy, amazon_jobs, workday, successfactors, phenom, join, taleo, icims, email, company_site, unknown
    needs_account: int = 0  # 0 or 1

    # Brief 9 additive columns
    years_required: Optional[int] = None
    contract_type: Optional[str] = None  # permanent, fixed_term, part_time, intern, freelance, unknown
    german_required: int = 0  # 0 or 1
    title_weight: float = 0.0

    # Start-up tracking (Brief 7)
    is_startup: int = 0  # 0 or 1
    startup_source: Optional[str] = None
    last_liveness_check: Optional[datetime] = None

    # Pipeline & application tracking
    exported_at: Optional[datetime] = None
    is_notified: bool = False
    user_status: str = "new"  # "new", "saved", "applied", "archived"
    applied_at: Optional[datetime] = None
    notes: Optional[str] = None


