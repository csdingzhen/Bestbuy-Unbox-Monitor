"""Discord formatting and delivery.

`build_embed` returns a plain dict in Discord's embed shape, so a bot that
already owns its own Discord connection can post it with discord.py
(`await channel.send(embed=discord.Embed.from_dict(embed))`) without taking a
dependency on anything here. `send_webhook` is only for standalone runs.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

from .detect import (
    REASON_LADDER,
    REASON_NEW_OFFER,
    REASON_PRICE_DROP,
    REASON_STRONG,
    DealEvent,
)

log = logging.getLogger(__name__)

COLORS = {
    REASON_STRONG: 0xD92D20,     # red
    REASON_NEW_OFFER: 0xF79009,  # amber
    REASON_PRICE_DROP: 0xF79009,
    REASON_LADDER: 0x475467,     # grey
}

REASON_LABELS = {
    REASON_STRONG: "Deep discount",
    REASON_NEW_OFFER: "New open-box offer",
    REASON_PRICE_DROP: "Price drop",
    REASON_LADDER: "Condition ladder detached",
}


def build_embed(event: DealEvent) -> dict:
    """Render a DealEvent as a Discord embed dict."""
    fields = [
        {
            "name": "Open-box price",
            "value": "**$%s**  (%s)" % (f"{event.price:,.2f}", event.condition),
            "inline": True,
        },
        {
            "name": "New price",
            "value": "$%s" % f"{event.new_price:,.2f}",
            "inline": True,
        },
        {
            "name": "Saving",
            "value": "**%.0f%%**  ($%s)" % (event.pct_off, f"{event.dollars_off:,.0f}"),
            "inline": True,
        },
    ]
    if event.previous_price:
        fields.append(
            {
                "name": "Was",
                "value": "$%s" % f"{event.previous_price:,.2f}",
                "inline": True,
            }
        )
    if event.ladder_gap:
        fields.append(
            {
                "name": "Ladder gap",
                "value": "%.1f%% (normal ~3%%)" % event.ladder_gap,
                "inline": True,
            }
        )
    fields.append({"name": "SKU", "value": event.sku, "inline": True})

    embed = {
        "title": (event.name or event.sku)[:250],
        "url": event.url,
        "description": "%s - ships to home" % REASON_LABELS.get(event.reason, event.reason),
        "color": COLORS.get(event.reason, 0x475467),
        "fields": fields,
        "footer": {"text": "Best Buy open-box - %s" % event.reason},
    }
    if event.image:
        embed["thumbnail"] = {"url": event.image}
    return embed


def build_payload(events: list[DealEvent], mention: Optional[str] = None) -> dict:
    """Bundle up to 10 events into one webhook payload (Discord's embed cap)."""
    payload: dict = {"embeds": [build_embed(e) for e in events[:10]]}
    if mention:
        payload["content"] = mention
    return payload


def send_webhook(
    webhook_url: str,
    events: list[DealEvent],
    mention: Optional[str] = None,
    timeout: int = 15,
) -> bool:
    """POST events to a Discord webhook. Returns True on success."""
    if not events:
        return True
    body = json.dumps(build_payload(events, mention)).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "openbox-monitor/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        log.error("discord webhook failed: %s", exc)
        return False
