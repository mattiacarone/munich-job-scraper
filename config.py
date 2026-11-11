import json
import logging
import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LIVE_MEMORY_DIR = Path(r"C:\Users\Mattia\Desktop\Job Application material\memory")

KEPT_ATS_SOURCES = {
    "greenhouse",
    "ashby",
    "lever",
    "personio",
    "workable",
    "recruitee",
    "smartrecruiters",
    "teamtailor",
}


def get_applied_history_path() -> Path:
    """Brief 14 B3: Read applied_history.csv ONLY from LIVE_MEMORY_DIR; never fall back to data/."""
    p = LIVE_MEMORY_DIR / "applied_history.csv"
    if not p.exists():
        logger.warning(f"[Config] Live applied_history.csv not found at {p}; running gate without it.")
    return p


def get_ger_skip_list_path() -> Path:
    """Brief 14 B3: Read ger_skip_list.csv ONLY from LIVE_MEMORY_DIR; never fall back to data/."""
    p = LIVE_MEMORY_DIR / "ger_skip_list.csv"
    if not p.exists():
        logger.warning(f"[Config] Live ger_skip_list.csv not found at {p}; running gate without it.")
    return p


def get_ats_registry_path() -> Path:
    p = LIVE_MEMORY_DIR / "ats_registry.json"
    return p if p.exists() else DATA_DIR / "ats_registry.json"


def get_linkedin_queries_path() -> Path:
    p = LIVE_MEMORY_DIR / "linkedin_queries.txt"
    return p if p.exists() else DATA_DIR / "linkedin_queries.txt"


def get_outcomes_path() -> Path:
    p = LIVE_MEMORY_DIR / "outcomes.csv"
    return p if p.exists() else DATA_DIR / "outcomes.csv"


# Export paths as constants for external inspection
APPLIED_HISTORY = get_applied_history_path()
GER_SKIP_LIST = get_ger_skip_list_path()
ATS_REGISTRY = get_ats_registry_path()
LINKEDIN_QUERIES = get_linkedin_queries_path()
OUTCOMES = get_outcomes_path()


def load_ats_registry() -> dict[str, list[str]]:
    """
    Load ATS registry from live memory (or data/ fallback) and merge
    with data/discovered_boards.json if present.
    Brief 14 B1: Only keep active ATS sources.
    """
    reg_path = get_ats_registry_path()
    registry: dict[str, list[str]] = {}
    if reg_path.exists():
        try:
            with open(reg_path, "r", encoding="utf-8") as f:
                raw_reg = json.load(f)
                registry = {k: v for k, v in raw_reg.items() if k in KEPT_ATS_SOURCES}
        except Exception:
            pass

    disc_path = DATA_DIR / "discovered_boards.json"
    if disc_path.exists():
        try:
            with open(disc_path, "r", encoding="utf-8") as f:
                discovered = json.load(f)
                for ats_name, slugs in discovered.items():
                    if ats_name not in KEPT_ATS_SOURCES:
                        continue
                    current = set(registry.get(ats_name, []))
                    current.update(slugs)
                    registry[ats_name] = sorted(list(current))
        except Exception:
            pass

    return registry


class ProfileConfig(BaseModel):
    # Candidate Metadata
    candidate_name: str = "Mattia Carone"
    target_geography: str = "Munich, Germany (or Remote within Germany)"
    working_language_rule: str = "English is sufficient. German may be a plus, but roles requiring German are skipped."
    
    # --------------------------------------------------------------------------
    # TARGET ROLE LIBRARY (Extracted from 05_Mattia_Carone_Target_Roles.docx)
    # --------------------------------------------------------------------------
    target_roles: list[str] = Field(
        default_factory=lambda: [
            # v7 priority families (4 Nov 2026)
            "Technical Consultant",
            "Technology Delivery Consultant",
            "Product Consultant",
            # Integration & API Consulting
            "Digital Integration Consultant",
            "Enterprise Integration Consultant",
            "Integration Consultant",
            "Integration Architect",
            "Associate Integration Architect",
            "Integration Specialist",
            "Integration Analyst",
            "Systems Integration Consultant",
            "Application Integration Consultant",
            "API Consultant",
            "API Integration Consultant",
            "API Management Consultant",
            "API Security Consultant",
            "MuleSoft Consultant",
            "MuleSoft Integration Consultant",
            "MuleSoft Technical Consultant",
            "MuleSoft Functional Consultant",
            "MuleSoft Developer",
            "Boomi Consultant",
            "Boomi Integration Consultant",
            "Boomi Developer",
            "iPaaS Consultant",
            "Cloud Integration Consultant",

            # Solutions Engineering & Pre-Sales
            "Solutions Engineer",
            "Solution Engineer",
            "Solution Consultant",
            "Solutions Consultant",
            "Pre-Sales Solutions Consultant",
            "Pre-Sales Consultant",
            "Pre-Sales Engineer",
            "Technical Solutions Consultant",
            "Customer Solutions Consultant",
            "Customer Solutions Engineer",
            "Technical Sales Engineer",
            "Sales Engineer",
            "SaaS Solutions Consultant",

            # Customer Success & Enablement
            "Customer Success Architect",
            "Customer Success Engineer",
            "Customer Success Manager",
            "Customer Success Consultant",
            "Enterprise Customer Success Manager",
            "Technical Customer Success Manager",
            "Technical Account Manager",
            "Customer Enablement Consultant",
            "Client Success Manager",
            "Client Enablement Lead",

            # AI & Emerging Technology
            "AI Consultant",
            "AI Solutions Consultant",
            "AI Implementation Consultant",
            "AI Transformation Consultant",
            "AI Adoption Consultant",
            "Agentic AI Consultant",
            "AI Integration Consultant",
            "AI Product Consultant",
            "AI Enablement Consultant",
            "AI Security Consultant",
            "Emerging Technology Consultant",
            "Innovation Consultant",

            # Enterprise Platforms (Salesforce / ServiceNow / HubSpot)
            "Salesforce Consultant",
            "Salesforce Integration Consultant",
            "Salesforce Business Analyst",
            "CRM Consultant",
            "CRM Business Analyst",
            "ServiceNow Consultant",
            "ServiceNow Implementation Consultant",
            "ServiceNow Business Analyst",
            "HubSpot Consultant",
            "HubSpot Implementation Consultant",
            "Marketing Technology Consultant",
            "CRM & Marketing Automation Consultant",

            # Business & Functional Consulting
            "Business Analyst",
            "Technical Business Analyst",
            "IT Business Analyst",
            "Digital Business Analyst",
            "Business Systems Analyst",
            "Functional Analyst",
            "Functional Consultant",
            "Technology Consultant",
            "Digital Transformation Consultant",
            "Implementation Consultant",
            "Technical Implementation Consultant",
            "SaaS Implementation Consultant",
            "Professional Services Consultant",
            "Client Delivery Consultant",
            "Client Solutions Consultant",
            "Technology Advisory Consultant",
            "Business Transformation Consultant",
            "Consultant - Digital Integration",
            "Consultant - Technology Transformation",
            "Consultant - Enterprise Applications",
            "Consultant - AI & Automation",
            "Consultant - API Management",
            "Consultant - Integration & Middleware",
            "Consultant - CRM & Salesforce",

            # Start-up Generalist & Operations (Brief 7)
            # 4 Nov 2026 (Mattia): software, backend/Python and AI developer roles added; broad ops/generalist titles removed
            "Software Engineer",
            "Software Developer",
            "Junior Software Engineer",
            "Python Developer",
            "Python Engineer",
            "Backend Engineer",
            "Backend Developer",
            "AI Engineer",
            "AI Developer",
            "AI Software Engineer",
            "Generative AI Engineer",
            "LLM Engineer",
            "AI Agent Engineer",
            "Applied AI Engineer",
            "Integration Developer",
            "API Developer",
            "Founding Customer Success Manager",
            "Founding Customer Success Engineer",
            "Founding Solutions Engineer",
            "Founding Implementation Manager",
        ]
    )

    # --------------------------------------------------------------------------
    # ATS KEYWORD BANK (Categorized by Domain & Weighted)
    # --------------------------------------------------------------------------
    # Tier 1: Core Integration & iPaaS (Mattia's core technical anchor)
    integration_keywords: dict[str, int] = Field(
        default_factory=lambda: {
            "MuleSoft": 20,
            "Anypoint Platform": 20,
            "Anypoint": 18,
            "Boomi": 20,
            "AtomSphere": 20,
            "DataWeave": 18,
            "API-led connectivity": 18,
            "API-led": 15,
            "CloudHub": 16,
            "CloudHub 2.0": 18,
            "CloudHub 1.0": 15,
            "iPaaS": 16,
            "RAML": 14,
            "API lifecycle": 12,
            "event-driven architecture": 12,
            "event-driven": 10,
            "integration design": 12,
            "systems integration": 10,
            "API management": 12,
            "middleware": 10,
            "REST APIs": 8,
            "REST API": 8,
            "SOQL": 8,
            "APIkit": 10,
        }
    )

    # Tier 2: Enterprise SaaS Platforms
    platform_keywords: dict[str, int] = Field(
        default_factory=lambda: {
            "Salesforce": 15,
            "ServiceNow": 15,
            "HubSpot": 15,
            "CRM": 8,
            "SaaS": 6,
            "enterprise applications": 8,
        }
    )

    # Tier 3: AI & Security (Emerging focus)
    ai_security_keywords: dict[str, int] = Field(
        default_factory=lambda: {
            "Agentic AI": 18,
            "Model Context Protocol": 20,
            "MCP": 16,
            "API security": 16,
            "Akamai": 16,
            "AI transformation": 14,
            "Generative AI": 12,
            "GenAI": 12,
            "AI integration": 14,
            "AI security": 14,
            "AI adoption": 12,
            "agent-to-agent": 14,
            "emerging technology": 8,
        }
    )

    # Tier 4: Business, Functional & Customer Consulting
    consulting_keywords: dict[str, int] = Field(
        default_factory=lambda: {
            "requirements analysis": 14,
            "business requirements": 12,
            "functional requirements": 12,
            "discovery workshops": 14,
            "workshops": 10,
            "stakeholder management": 12,
            "business analysis": 10,
            "solution design": 12,
            "implementation consulting": 14,
            "client delivery": 10,
            "UAT": 12,
            "user stories": 10,
            "cross-functional collaboration": 8,
            "technical-to-business translation": 15,
            "business-to-technology translation": 15,
            "solution consulting": 15,
            "pre-sales": 14,
            "presales": 14,
            "solution presentations": 12,
            "customer success": 12,
            "client enablement": 12,
            "go-to-market": 10,
            "GTM": 10,
            "client onboarding": 10,
        }
    )

    # Tier 5: Delivery, DevOps & Tooling
    tooling_keywords: dict[str, int] = Field(
        default_factory=lambda: {
            "Jenkins": 6,
            "CI/CD": 6,
            "Git": 5,
            "GitLab": 6,
            "Maven": 6,
            "Postman": 6,
            "Splunk": 6,
            "CloudWatch": 6,
            "Azure": 6,
            "Python": 6,
            "Agile": 4,
            "Scrum": 4,
            "MUnit": 8,
        }
    )

    # --------------------------------------------------------------------------
    # NEGATIVE FILTERS & DISQUALIFIERS
    # --------------------------------------------------------------------------
    # Roles/Seniority to ignore
    negative_keywords: list[str] = Field(
        default_factory=lambda: [
            "Werkstudent",
            "Working Student",
            "Praktikum",
            "Praktikant",
            "Internship",
            "Intern",
            "Trainee",
            "Ausbildung",
            "Auszubildende",
            "Pflegekraft",
            "Sachbearbeiter",
            "Bauleiter",
            # Hardware/low-level developer roles that clash with functional/integration consulting
            "Embedded Systems",
            "Firmware Developer",
            "Hardware Engineer",
            "PLC Developer",
            "SPS-Programmierer",
            "C++ Game Developer",
            "Kernel Developer",
            # Brief 13 Section 4: additional negative keywords
            "Construction",
            "Bauarbeiter",
            "Site Manager",
            "Electrician",
            "Mechanic",
            "Dental",
            "Recruiter",
            "Recruitment Consultant",
            "Procurement",
            "Purchasing",
            "Thermal",
            "Propulsion",
            "Powertrain",
        ]
    )

    # Target Locations (Munich / Bavaria / Remote Germany)
    target_locations: list[str] = Field(
        default_factory=lambda: [
            "Munich",
            "München",
            "Munich Area",
            "Großraum München",
            "Bayern",
            "Bavaria",
            "Gilching",
            "Garching",
            "Unterföhring",
            "Remote",
            "Remote in Germany",
            "Home-Office (Deutschland)",
            "Germany",
            "Deutschland",
        ]
    )

    # Scoring Threshold
    # Minimum score required to trigger an alert / notification
    min_score_threshold: int = 25


class AppConfig(BaseModel):
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    
    # Scheduler interval
    poll_interval_minutes: int = 20
    schedule_jitter_seconds: int = 60

    # Persistence
    db_path: Path = BASE_DIR / "jobs.db"
    APPLIED_HISTORY: Path = Field(default_factory=get_applied_history_path)
    GER_SKIP_LIST: Path = Field(default_factory=get_ger_skip_list_path)

    # Notification Webhooks & Telegram (pulled from .env / environment)
    slack_webhook_url: str = Field(default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL", ""))
    discord_webhook_url: str = Field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", ""))
    telegram_bot_token: str = Field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = Field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))


config = AppConfig()

