"""Compatibility imports for provider-specific scene scraper implementations."""
from __future__ import annotations

try:
    from sync.scene_scraper_algolia import AlgoliaScraper
    from sync.scene_scraper_belami import BelAmiScraper
    from sync.scene_scraper_helix import HelixScraper
    from sync.scene_scraper_lucas import LucasScraper
    from sync.scene_scraper_project1 import Project1Scraper
except ModuleNotFoundError:
    from scene_scraper_algolia import AlgoliaScraper
    from scene_scraper_belami import BelAmiScraper
    from scene_scraper_helix import HelixScraper
    from scene_scraper_lucas import LucasScraper
    from scene_scraper_project1 import Project1Scraper

__all__ = ["AlgoliaScraper", "BelAmiScraper", "HelixScraper", "LucasScraper", "Project1Scraper"]
