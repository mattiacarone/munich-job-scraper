"""
Ecosystem Partners Seeding & Probing Module (Brief 11 Section 4).
Curates partner firms for Mattia's core platform skills:
MuleSoft, Boomi, Workato, Salesforce, ServiceNow, HubSpot, and Celonis.
Stores partner companies in the SQLite `companies` table and probes them
via BoardProber to discover their career ATS boards.
"""

from datetime import datetime, timezone
import logging
import re
from typing import Optional
from database import Database
from prober import BoardProber

logger = logging.getLogger(__name__)

# Key German and EMEA integration, consulting, and implementation partners
# for MuleSoft, Boomi, Workato, Salesforce, ServiceNow, HubSpot, and Celonis.
ECOSYSTEM_PARTNERS = [
    # MuleSoft & Boomi & Workato integration partners
    {"name": "Devoteam", "ecosystem": "mulesoft,workato,servicenow"},
    {"name": "Slalom", "ecosystem": "mulesoft,boomi,salesforce"},
    {"name": "valantic", "ecosystem": "mulesoft,workato,salesforce"},
    {"name": "WHISHWORKS", "ecosystem": "mulesoft"},
    {"name": "VRP Consulting", "ecosystem": "mulesoft,salesforce"},
    {"name": "codecentric", "ecosystem": "mulesoft,integration"},
    {"name": "Adesso", "ecosystem": "mulesoft,integration"},
    {"name": "Senacor", "ecosystem": "mulesoft,integration"},
    {"name": "Persistent Systems", "ecosystem": "mulesoft,salesforce"},
    {"name": "Reply", "ecosystem": "mulesoft,salesforce,servicenow"},
    {"name": "BearingPoint", "ecosystem": "mulesoft,salesforce"},
    {"name": "CGI", "ecosystem": "mulesoft,salesforce"},
    {"name": "Kitepipe", "ecosystem": "boomi"},
    {"name": "Jade Global", "ecosystem": "boomi,salesforce"},
    {"name": "Dispatch Integration", "ecosystem": "workato"},
    {"name": "Omniaz", "ecosystem": "workato"},
    # Salesforce AppExchange Munich & DACH consultants
    {"name": "Factory42", "ecosystem": "salesforce"},
    {"name": "CloudMonki", "ecosystem": "salesforce"},
    {"name": "Cloudwharf", "ecosystem": "salesforce"},
    {"name": "DIGITALL", "ecosystem": "salesforce"},
    {"name": "EMPAUA", "ecosystem": "salesforce"},
    {"name": "PARX", "ecosystem": "salesforce"},
    {"name": "Fluido", "ecosystem": "salesforce"},
    # ServiceNow DACH partners
    {"name": "Materna", "ecosystem": "servicenow"},
    {"name": "Computacenter", "ecosystem": "servicenow"},
    {"name": "Cancom", "ecosystem": "servicenow"},
    {"name": "SVA System Vertrieb Alexander", "ecosystem": "servicenow"},
    {"name": "sepago", "ecosystem": "servicenow"},
    # HubSpot solutions partners
    {"name": "TRIALTA", "ecosystem": "hubspot"},
    {"name": "W4 Marketing", "ecosystem": "hubspot"},
    {"name": "Projecter", "ecosystem": "hubspot"},
    {"name": "Inbound Consulting", "ecosystem": "hubspot"},
    # Celonis ecosystem
    {"name": "Celonis", "ecosystem": "celonis"},
    {"name": "ProcessGold", "ecosystem": "celonis"},
    {"name": "Camelot ITLab", "ecosystem": "celonis"},
    {"name": "Protiviti", "ecosystem": "celonis"},
]


def normalise_slug(name: str) -> str:
    """Create URL-safe slug from company name."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def seed_ecosystem_partners(db: Database) -> int:
    """
    Seed partner companies into SQLite `companies` table.
    Returns number of seeded rows.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    inserted = 0

    with db._get_connection() as conn:
        for partner in ECOSYSTEM_PARTNERS:
            name = partner["name"]
            slug = normalise_slug(name)
            src = f"partner_{partner['ecosystem'].split(',')[0]}"
            conn.execute(
                """
                INSERT INTO companies (source, slug, display_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, slug) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = excluded.updated_at
                """,
                (src, slug, name, now_iso),
            )
            inserted += 1

    logger.info(f"[EcosystemPartners] Seeded {inserted} partners into `companies` table.")
    return inserted


def probe_ecosystem_partners(db: Database, prober: Optional[BoardProber] = None, max_probe: int = 15) -> int:
    """
    Probe seeded ecosystem partners across all supported ATS (Brief 12: shared discovery step).
    `prober` is accepted for backwards compatibility and ignored.
    """
    from company_discovery import discover_names

    with db._get_connection() as conn:
        cursor = conn.execute(
            "SELECT display_name FROM companies WHERE source LIKE 'partner_%' LIMIT ?",
            (max_probe * 4,),
        )
        names = [row["display_name"] for row in cursor.fetchall()]
    res = discover_names(db, names, max_names=max_probe)
    return res["probed"]

