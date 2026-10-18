"""Parse Best Buy product pages into open-box offer snapshots.

Everything needed is server-rendered into the page's Apollo cache, so a single
GET of https://www.bestbuy.com/site/-/{sku}.p?skuId={sku} yields the new price,
every open-box condition tier and each tier's price.

Fulfillment is rendered only for the *selected* condition, so purchasability and
shipping eligibility for a given tier come from that tier's own page:
    https://www.bestbuy.com/product/a/{productId}/sku/{sku}/openbox?condition=good

The authoritative signal is fulfillmentOptions.buttonStates[], whose entries are
tagged with the condition they describe:

    {"buttonState":"ADD_TO_CART",  "condition":"OPEN_BOX_EXCELLENT"}  -> shippable
    {"buttonState":"CHECK_STORES", "condition":"OPEN_BOX_GOOD"}       -> pickup only
    {"buttonState":"SOLD_OUT",     "condition":"OPEN_BOX_FAIR"}       -> gone
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# openBoxCondition codes as used by Best Buy's GraphQL schema.
CONDITION_NAMES = {0: "Fair", 1: "Good", 2: "Excellent"}
CONDITION_CODES = {v.lower(): k for k, v in CONDITION_NAMES.items()}
CONDITION_TAGS = {name: "OPEN_BOX_" + name.upper() for name in CONDITION_NAMES.values()}

#: buttonState meaning "you can add this to a cart and have it shipped".
BUYABLE_STATE = "ADD_TO_CART"
#: buttonState meaning the unit exists but only for in-store pickup.
PICKUP_ONLY_STATE = "CHECK_STORES"

_RE_NAME = re.compile(r'"short":"((?:[^"\\]|\\.)*)"')
_RE_PRODUCT_ID = re.compile(r'/product/[^/"]+/([A-Z0-9]{8,12})/sku/(\d+)')
_RE_IMAGE = re.compile(r'"piscesHref":"(https://pisces\.bbystatic\.com/[^"]+)"')

# One InboundBuyingOption: tier, price, deep link.
_RE_BUYING_OPTION = re.compile(
    r'"(?:pdpUrl|url)":"(?P<url>https://www\.bestbuy\.com/product/[^"]*?/sku/(?P<usku>\d+)'
    r'/openbox\?condition=(?P<cond>\w+))"'
    r'.{0,80}?"skuId":"(?P<sku>\d+)","type":"(?P<type>New|Excellent|Good|Fair)"'
    r'.{0,200}?"customerPrice":(?P<price>[0-9.]+)',
    re.S,
)

_RE_NEW_OPTION = re.compile(
    r'"skuId":"(?P<sku>\d+)","type":"New","product":\{"price":\{"customerPrice":(?P<price>[0-9.]+)',
    re.S,
)

# A buttonStates[] entry, tagged with the condition it applies to.
_RE_BUTTON_STATE = re.compile(
    r'"buttonState":"(?P<state>\w+)","condition":"(?P<cond>\w+)"'
)

_RE_SHIPPING_ELIGIBLE = re.compile(r'"shippingEligible":(true|false)')


@dataclass
class Offer:
    """One open-box condition tier for one SKU."""

    condition: str  # "Excellent" | "Good" | "Fair"
    code: int  # 2 | 1 | 0
    price: float
    url: Optional[str] = None
    #: buttonState for this tier, when known (only its own condition page is authoritative).
    button_state: Optional[str] = None

    @property
    def buyable(self) -> bool:
        """Can be added to cart and shipped."""
        return self.button_state == BUYABLE_STATE

    @property
    def pickup_only(self) -> bool:
        """Exists in a store but cannot be shipped."""
        return self.button_state == PICKUP_ONLY_STATE

    @property
    def confirmed(self) -> bool:
        """True once we have fetched this tier's own page."""
        return self.button_state is not None

    @property
    def key(self) -> str:
        return "%s:%.2f" % (self.condition, self.price)


@dataclass
class Snapshot:
    """What we know about one SKU at one point in time."""

    sku: str
    name: str = ""
    product_id: Optional[str] = None
    new_price: Optional[float] = None
    image: Optional[str] = None
    offers: list[Offer] = field(default_factory=list)

    def offer(self, condition: str) -> Optional[Offer]:
        for o in self.offers:
            if o.condition == condition:
                return o
        return None

    @property
    def shippable_offers(self) -> list[Offer]:
        return [o for o in self.offers if o.buyable]

    @property
    def best_offer(self) -> Optional[Offer]:
        live = self.shippable_offers
        return min(live, key=lambda o: o.price) if live else None

    def pct_off(self, offer: Offer) -> Optional[float]:
        if not self.new_price or self.new_price <= 0:
            return None
        return (self.new_price - offer.price) / self.new_price * 100.0

    def dollars_off(self, offer: Offer) -> Optional[float]:
        if not self.new_price:
            return None
        return round(self.new_price - offer.price, 2)

    def product_url(self) -> str:
        if self.product_id:
            return "https://www.bestbuy.com/product/a/%s/sku/%s" % (self.product_id, self.sku)
        return "https://www.bestbuy.com/site/-/%s.p?skuId=%s" % (self.sku, self.sku)

    def condition_url(self, condition: str) -> str:
        if self.product_id:
            return "https://www.bestbuy.com/product/a/%s/sku/%s/openbox?condition=%s" % (
                self.product_id,
                self.sku,
                condition.lower(),
            )
        return self.product_url()

    def ladder_gap(self) -> Optional[float]:
        """Largest % gap between adjacent condition tiers.

        Normal, algorithmically-pegged open-box pricing holds tiers ~3% apart
        because every tier derives from the same live new price. A gap above
        ~8% means the ladder has detached from the new price, which is the
        fingerprint of a batch re-peg / liquidation event rather than a routine
        markdown.
        """
        prices = sorted(o.price for o in self.offers)
        if len(prices) < 2:
            return None
        return max((hi - lo) / hi * 100.0 for lo, hi in zip(prices, prices[1:]) if hi > 0)


def button_states(html: str) -> dict[str, str]:
    """Map condition tag (e.g. "OPEN_BOX_GOOD") -> buttonState."""
    states: dict[str, str] = {}
    for m in _RE_BUTTON_STATE.finditer(html):
        tag, state = m.group("cond"), m.group("state")
        # A tier listed as buyable anywhere on the page wins over SOLD_OUT.
        if states.get(tag) != BUYABLE_STATE:
            states[tag] = state
    return states


def shipping_eligible(html: str) -> Optional[bool]:
    """Best-effort read of shippingEligible; None when absent."""
    found = _RE_SHIPPING_ELIGIBLE.findall(html)
    if not found:
        return None
    return "true" in found


def parse(html: str, sku: str) -> Snapshot:
    """Build a Snapshot for `sku` from a fetched Best Buy page."""
    snap = Snapshot(sku=sku)

    for pid, psku in _RE_PRODUCT_ID.findall(html):
        if psku == sku:
            snap.product_id = pid
            break

    name = _RE_NAME.search(html)
    if name:
        snap.name = name.group(1).replace('\\"', '"').replace("\\/", "/")

    image = _RE_IMAGE.search(html)
    if image:
        snap.image = image.group(1)

    new_prices = [float(p) for s, p in _RE_NEW_OPTION.findall(html) if s == sku]
    if new_prices:
        snap.new_price = max(new_prices)

    states = button_states(html)

    best_by_cond: dict[str, Offer] = {}
    for m in _RE_BUYING_OPTION.finditer(html):
        if m.group("sku") != sku or m.group("usku") != sku:
            continue
        tier = m.group("type")
        if tier == "New":
            continue
        code = CONDITION_CODES.get(m.group("cond").lower())
        if code is None:
            continue
        offer = Offer(
            condition=tier,
            code=code,
            price=float(m.group("price")),
            url=m.group("url"),
            button_state=states.get(CONDITION_TAGS[tier]),
        )
        prev = best_by_cond.get(tier)
        if prev is None or offer.price < prev.price:
            best_by_cond[tier] = offer

    snap.offers = sorted(best_by_cond.values(), key=lambda o: -o.code)
    return snap


def apply_condition_page(snap: Snapshot, html: str, condition: str) -> Snapshot:
    """Fold a condition-specific page's fulfillment data into `snap`.

    Only the fetched tier's buttonState is authoritative on that page, so only
    that tier is updated.
    """
    offer = snap.offer(condition)
    if offer is None:
        return snap
    states = button_states(html)
    state = states.get(CONDITION_TAGS[condition])
    if state:
        offer.button_state = state
    # Refresh the price too; the condition page is the most specific source.
    fresh = parse(html, snap.sku).offer(condition)
    if fresh and fresh.price:
        offer.price = fresh.price
    return snap
