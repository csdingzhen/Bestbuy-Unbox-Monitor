#!/usr/bin/env python3
"""CLI entrypoint for the Best Buy open-box monitor.

    python run.py check 6613959        inspect one SKU and exit
    python run.py discover             build the watchlist, then exit
    python run.py scan                 one scan pass, print deals
    python run.py watch                run forever, post to Discord
"""
from __future__ import annotations

import argparse
import logging
import sys

from openbox_monitor import Monitor, build_embed, load_config, send_webhook
from openbox_monitor.client import BestBuyClient
from openbox_monitor.parser import apply_condition_page, parse


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_check(args) -> int:
    """Fetch one SKU and print its full open-box state."""
    cfg = load_config(args.config)
    client = BestBuyClient(
        rate_per_sec=float(cfg["requests_per_second"]),
        zip_code=cfg.get("zip_code") or None,
    )
    html = client.fetch_product(args.sku)
    if not html:
        print("could not fetch SKU %s" % args.sku)
        return 1

    snap = parse(html, args.sku)
    print("sku       : %s" % snap.sku)
    print("name      : %s" % snap.name)
    print("productId : %s" % snap.product_id)
    print("new price : %s" % snap.new_price)
    gap = snap.ladder_gap()
    print("ladder gap: %s" % ("%.1f%%" % gap if gap else "n/a"))

    # A manual inspection is cheap, so confirm every tier rather than only the
    # ones a scan pass would spend a request on.
    for offer in list(snap.offers):
        if snap.product_id:
            page = client.fetch_condition(args.sku, snap.product_id, offer.condition)
            if page:
                apply_condition_page(snap, page, offer.condition)

    if not snap.offers:
        print("\nno open-box offers")
        return 0

    print("\n%-10s %12s %9s  %-14s %s" % ("tier", "price", "% off", "buttonState", "shippable"))
    print("-" * 68)
    for offer in snap.offers:
        pct = snap.pct_off(offer)
        print(
            "%-10s %12s %9s  %-14s %s"
            % (
                offer.condition,
                "$%s" % f"{offer.price:,.2f}",
                "%.1f%%" % pct if pct is not None else "n/a",
                offer.button_state or "(unconfirmed)",
                "yes" if offer.buyable else ("pickup only" if offer.pickup_only else "no"),
            )
        )
    return 0


def cmd_discover(args) -> int:
    mon = Monitor.from_config(args.config)
    added = mon.refresh_watchlist(force=True)
    print("added %d new SKUs; watchlist is now %d" % (added, mon.store.count()))
    mon.close()
    return 0


def cmd_scan(args) -> int:
    mon = Monitor.from_config(args.config)
    events = mon.scan_once()
    if not events:
        print("no deals this pass")
    for event in events:
        print(
            "[%s] %-9s %6.1f%% off  $%-10s (new $%s)  %s"
            % (
                event.reason,
                event.condition,
                event.pct_off,
                f"{event.price:,.2f}",
                f"{event.new_price:,.2f}",
                event.name[:52],
            )
        )
        print("         %s" % event.url)
    webhook = mon.config.get("discord_webhook")
    if events and webhook and args.post:
        send_webhook(webhook, events, mon.config.get("discord_mention") or None)
        print("posted %d deal(s) to Discord" % len(events))
    mon.close()
    return 0


def cmd_watch(args) -> int:
    mon = Monitor.from_config(args.config)
    webhook = mon.config.get("discord_webhook")
    mention = mon.config.get("discord_mention") or None
    if not webhook:
        print("warning: no discord_webhook configured; printing to stdout only")
    print("watching %d SKUs; ctrl-c to stop" % mon.store.count())
    try:
        for event in mon.stream():
            print(
                "[%s] %.1f%% off  $%s  %s"
                % (event.reason, event.pct_off, f"{event.price:,.2f}", event.name[:60])
            )
            if webhook:
                send_webhook(webhook, [event], mention)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        mon.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Best Buy open-box deal monitor")
    parser.add_argument("-c", "--config", default="config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="inspect a single SKU")
    p_check.add_argument("sku")
    p_check.set_defaults(func=cmd_check)

    p_disc = sub.add_parser("discover", help="build the SKU watchlist")
    p_disc.set_defaults(func=cmd_discover)

    p_scan = sub.add_parser("scan", help="run one scan pass")
    p_scan.add_argument("--post", action="store_true", help="post results to Discord")
    p_scan.set_defaults(func=cmd_scan)

    p_watch = sub.add_parser("watch", help="run continuously")
    p_watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
