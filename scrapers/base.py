"""
Base Scraper abstraction.
Defines standard request headers, hashing helpers for unique IDs, and lifecycle hooks.
"""

from abc import ABC, abstractmethod
import hashlib
from typing import Optional
import httpx
from models import JobPosting


class BaseScraper(ABC):
    def __init__(self, name: str):
        self.name = name
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        }

    def generate_job_id(self, url: str, title: str, company: str) -> str:
        """
        Creates a deterministic 16-character SHA-256 hash
        based on canonical URL and company to prevent duplicates across runs.
        """
        seed = f"{url.strip().lower()}|{company.strip().lower()}|{title.strip().lower()}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @abstractmethod
    def scrape(self) -> list[JobPosting]:
        """
        Main execution method returning a list of extracted JobPosting instances.
        Must be implemented by child scrapers.
        """
        pass

