"""Best Buy open-box deal monitor.

Finds shippable open-box units priced far below their new price, and ignores
pickup-only stock.

Typical embedding into an existing Discord bot::

    from openbox_monitor import Monitor, build_embed

    mon = Monitor.from_config("config.json")
    for event in mon.stream():
        await channel.send(embed=discord.Embed.from_dict(build_embed(event)))
"""
from .client import BestBuyClient
from .detect import DealEvent, Thresholds, evaluate
from .notify import build_embed, build_payload, send_webhook
from .parser import Offer, Snapshot, parse
from .runner import DEFAULT_CONFIG, Monitor, load_config
from .store import Store

__version__ = "1.0.0"

__all__ = [
    "BestBuyClient",
    "DealEvent",
    "Thresholds",
    "evaluate",
    "build_embed",
    "build_payload",
    "send_webhook",
    "Offer",
    "Snapshot",
    "parse",
    "Monitor",
    "load_config",
    "DEFAULT_CONFIG",
    "Store",
]
