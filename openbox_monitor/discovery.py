"""Build and refresh the SKU watchlist.

Best Buy's listing pages embed every result's skuId, but server-render the full
buyingOptions block for only the first few products. So listing pages are used
purely to harvest SKUs; open-box state always comes from the per-SKU scan.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from .client import BestBuyClient

log = logging.getLogger(__name__)

_RE_SKU = re.compile(r'"skuId":"(\d{6,8})"')

#: Listing pages covering high-value categories. `cp` is appended for paging.
DEFAULT_SOURCES = [
    # Open-box computers & tablets - the densest source of live open-box stock.
    "https://www.bestbuy.com/site/searchpage.jsp?browsedCategory=pcmcat1751036309117"
    "&id=pcat17071&st=pcmcat1751036309117_categoryid%24abcat0500000",
    # Gaming laptops.
    "https://www.bestbuy.com/site/computers-pcs/gaming-laptops/abcat0502001.c?id=abcat0502001",
    # Current-gen GPU laptops by search.
    "https://www.bestbuy.com/site/searchpage.jsp?st=rtx+5090+gaming+laptop",
    "https://www.bestbuy.com/site/searchpage.jsp?st=rtx+5080+gaming+laptop",
    "https://www.bestbuy.com/site/searchpage.jsp?st=rtx+5070+ti+gaming+laptop",
    # Desktop graphics cards.
    "https://www.bestbuy.com/site/searchpage.jsp?st=geforce+rtx+5090+graphics+card",
    "https://www.bestbuy.com/site/searchpage.jsp?st=geforce+rtx+5080+graphics+card",
    # Prebuilt gaming desktops.
    "https://www.bestbuy.com/site/computers-pcs/gaming-desktops/abcat0501000.c?id=abcat0501000",
    # High-end monitors.
    "https://www.bestbuy.com/site/searchpage.jsp?st=oled+gaming+monitor",
]


def harvest(client: BestBuyClient, url: str, pages: int = 1) -> set[str]:
    """Return every skuId appearing on `url` (and its first `pages` pages)."""
    found: set[str] = set()
    for page in range(1, max(1, pages) + 1):
        target = url if page == 1 else "%s%scp=%d" % (
            url,
            "&" if "?" in url else "?",
            page,
        )
        html = client.get(target)
        if not html:
            log.warning("discovery: no response for %s", target)
            break
        skus = set(_RE_SKU.findall(html))
        if not skus:
            break
        before = len(found)
        found |= skus
        # No new SKUs means we have run off the end of the result set.
        if len(found) == before:
            break
    return found


def discover(
    client: BestBuyClient,
    sources: Iterable[str] | None = None,
    pages_per_source: int = 3,
) -> dict[str, set[str]]:
    """Harvest SKUs from every configured source.

    Returns a mapping of source url -> SKUs, so callers can record provenance.
    """
    out: dict[str, set[str]] = {}
    for url in sources or DEFAULT_SOURCES:
        skus = harvest(client, url, pages_per_source)
        out[url] = skus
        log.info("discovery: %d SKUs from %s", len(skus), url[:70])
    return out
