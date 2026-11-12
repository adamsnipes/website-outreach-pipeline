"""Local fixture websites + the static server that makes them crawlable.

These exist so the entire pipeline -- discovery, crawling, rendering, scoring,
contact extraction, qualification, building, QA, composition and queueing --
can be exercised end to end with every external feature flag switched off.
"""

from outreach.fixtures.server import FixtureSiteServer, fixture_sites_root

__all__ = ["FixtureSiteServer", "fixture_sites_root"]
