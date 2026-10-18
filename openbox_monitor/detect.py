"""Deal detection rules.

What makes an open-box listing interesting is not its standing discount. Best
Buy pegs open-box prices algorithmically to the live new price, so the ordinary
state of the world is 5-12% off with condition tiers about 3% apart. Watching
for "% off" alone produces constant noise and still misses the real events.

The deals worth catching are *transitions*: a tier that was absent or sold out
becomes purchasable at a price far below the new price, because the whole
ladder has been re-pegged to a liquidation reference. Those events show two
tells at once - a large absolute discount, and a condition ladder that has
widened well beyond its usual ~3%.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .parser import Offer, Snapshot

# Reason codes, most to least urgent.
REASON_STRONG = "STRONG_DISCOUNT"
REASON_NEW_OFFER = "NEW_OFFER"
REASON_PRICE_DROP = "PRICE_DROP"
REASON_LADDER = "LADDER_ANOMALY"

SEVERITY = {
    REASON_STRONG: 3,
    REASON_NEW_OFFER: 2,
    REASON_PRICE_DROP: 2,
    REASON_LADDER: 1,
}


@dataclass
class Thresholds:
    #: Minimum discount off the new price to be worth an alert at all.
    alert_pct: float = 35.0
    #: Discount that always alerts, even without a state transition.
    strong_pct: float = 45.0
    #: Condition-ladder gap that marks a detached/liquidation ladder.
    ladder_gap_pct: float = 8.0
    #: Price drop versus the last scan that alerts on its own.
    price_drop_pct: float = 10.0
    #: Ignore anything cheaper than this new price.
    min_new_price: float = 400.0
    #: Ignore savings below this many dollars.
    min_dollars_off: float = 150.0


@dataclass
class DealEvent:
    """A single alertable open-box offer."""

    sku: str
    name: str
    condition: str
    price: float
    new_price: float
    pct_off: float
    dollars_off: float
    reason: str
    url: str
    image: Optional[str] = None
    ladder_gap: Optional[float] = None
    previous_price: Optional[float] = None

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.reason, 1)

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "condition": self.condition,
            "price": self.price,
            "new_price": self.new_price,
            "pct_off": round(self.pct_off, 1),
            "dollars_off": round(self.dollars_off, 2),
            "reason": self.reason,
            "severity": self.severity,
            "url": self.url,
            "image": self.image,
            "ladder_gap": round(self.ladder_gap, 1) if self.ladder_gap else None,
            "previous_price": self.previous_price,
        }


def evaluate(
    snap: Snapshot,
    offer: Offer,
    thresholds: Thresholds,
    previous_price: Optional[float] = None,
    previously_buyable: bool = False,
) -> Optional[DealEvent]:
    """Decide whether `offer` warrants an alert.

    Only shippable offers qualify; pickup-only units (buttonState CHECK_STORES)
    and sold-out tiers are ignored.
    """
    if not offer.buyable:
        return None
    if not snap.new_price or snap.new_price < thresholds.min_new_price:
        return None

    pct = snap.pct_off(offer)
    dollars = snap.dollars_off(offer)
    if pct is None or dollars is None:
        return None
    if dollars < thresholds.min_dollars_off:
        return None

    gap = snap.ladder_gap()
    reason: Optional[str] = None

    if pct >= thresholds.strong_pct:
        reason = REASON_STRONG
    elif pct >= thresholds.alert_pct:
        # Above the bar, but only interesting if something actually changed.
        if not previously_buyable:
            reason = REASON_NEW_OFFER
        elif previous_price and previous_price > 0:
            drop = (previous_price - offer.price) / previous_price * 100.0
            if drop >= thresholds.price_drop_pct:
                reason = REASON_PRICE_DROP
        if reason is None and gap and gap >= thresholds.ladder_gap_pct:
            reason = REASON_LADDER
    elif gap and gap >= thresholds.ladder_gap_pct and not previously_buyable:
        # Ladder has detached even though this tier is not yet deeply cut -
        # often the leading edge of a re-peg, worth a low-severity heads-up.
        reason = REASON_LADDER

    if reason is None:
        return None

    return DealEvent(
        sku=snap.sku,
        name=snap.name,
        condition=offer.condition,
        price=offer.price,
        new_price=snap.new_price,
        pct_off=pct,
        dollars_off=dollars,
        reason=reason,
        url=offer.url or snap.condition_url(offer.condition),
        image=snap.image,
        ladder_gap=gap,
        previous_price=previous_price,
    )


def worth_confirming(snap: Snapshot, thresholds: Thresholds) -> list[Offer]:
    """Tiers cheap enough that it is worth spending a request to confirm them.

    The base product page gives every tier's price but renders fulfillment only
    for the selected condition. Rather than fetch all three condition pages for
    every SKU, fetch only the tiers whose price already looks alertable.
    """
    if not snap.new_price or snap.new_price < thresholds.min_new_price:
        return []
    # A ladder that has blown open is worth confirming even if no single tier
    # has crossed the discount bar yet.
    gap = snap.ladder_gap()
    ladder_hot = bool(gap and gap >= thresholds.ladder_gap_pct)

    out = []
    for offer in snap.offers:
        pct = snap.pct_off(offer)
        dollars = snap.dollars_off(offer)
        if pct is None or dollars is None:
            continue
        if dollars < thresholds.min_dollars_off:
            continue
        if pct >= thresholds.alert_pct or ladder_hot:
            out.append(offer)
    return out
