# Best Buy open-box monitor

Finds Best Buy open-box units priced far below their new price, and posts them
to Discord. Built to catch events like an ASUS ROG Zephyrus G16 / RTX 5080 /
32GB / 2TB dropping to **$1,276.99 against a $3,399.99 new price (-62%)**, or an
Alienware Area-51 18 / RTX 5090 / 64GB landing at **$2,234.99 against $5,949.99
(-62%)**.

**Shipping only.** Pickup-only open-box stock is detected and discarded.

---

## 1. What this is actually looking for

This matters, because the obvious implementation finds nothing.

Best Buy pegs open-box prices algorithmically to the *live* new price. The
ordinary state of the world is **5-12% off, with the Excellent/Good/Fair tiers
about 3% apart**. A monitor that alerts on "% off new" fires constantly at 8%
and still misses every deal worth having.

The real deals are **transitions**, not standing prices. A tier that was absent
or sold out becomes purchasable at a price that has detached from the new price
entirely — the whole condition ladder gets re-pegged to what looks like a
liquidation reference. Those events show two tells at once:

| | Normal pegged pricing | A real event |
|---|---|---|
| Discount off new | 5-12% | 45-62% |
| Gap between adjacent tiers | ~3% | 11%+ |

So the detector keys on the **state change plus the ladder gap**, not on a
threshold over a static value. `ladder_gap_pct` in the config is that second
tell; it is the earliest warning available and often precedes the deepest cuts.

The monitor is quiet most of the time. That is correct behaviour, not a fault.

---

## 2. Quick start

```bash
pip install -r requirements.txt
cp config.example.json config.json       # then set discord_webhook
python run.py discover                   # build the SKU watchlist (once)
python run.py watch                      # run forever
```

Verify it works against a single SKU before deploying:

```bash
python run.py check 6397846
```

```
sku       : 6397846
name      : Apple - MacBook Air 13-inch Laptop - M5 chip ...
productId : JJGCQLKXL7
new price : 1099.0
ladder gap: 3.2%

tier              price     % off  buttonState    shippable
--------------------------------------------------------------------
Excellent     $1,064.99      3.1%  ADD_TO_CART    yes
Good          $1,031.99      6.1%  CHECK_STORES   pickup only
Fair            $998.99      9.1%  CHECK_STORES   pickup only
```

That output is the whole design in miniature: the two cheaper tiers exist and
are priced, but are **pickup-only**, so they are suppressed. Naively alerting on
the lowest listed price would have produced two false alarms on this one SKU.

**Requirements:** Python 3.10+ and `curl_cffi`. Nothing else.

---

## 3. Integrating with an existing Discord bot

The bot owns the Discord connection; this package just produces events.
`build_embed` returns a plain dict in Discord's embed shape, so there is no
dependency on discord.py here.

```python
import asyncio
from openbox_monitor import Monitor, build_embed
import discord

mon = Monitor.from_config("config.json")

async def openbox_loop(channel: discord.TextChannel):
    while True:
        # scan_once() is blocking and network-bound; keep it off the event loop.
        events = await asyncio.to_thread(mon.scan_once)
        for event in events:
            await channel.send(embed=discord.Embed.from_dict(build_embed(event)))
        await asyncio.sleep(mon.config["cycle_seconds"])
```

If the bot has its own scheduler, call `mon.scan_once()` from it and ignore
`stream()` entirely.

### The event object

`scan_once()` returns `list[DealEvent]`, already sorted most urgent first, and
already de-duplicated against the alert log. Every event is **shippable** —
pickup-only offers never become events.

| Field | Meaning |
|---|---|
| `sku` | Best Buy SKU |
| `name` | Product title |
| `condition` | `Excellent` / `Good` / `Fair` |
| `price` | Open-box price |
| `new_price` | Current new price |
| `pct_off`, `dollars_off` | Saving versus new |
| `reason` | `STRONG_DISCOUNT` / `NEW_OFFER` / `PRICE_DROP` / `LADDER_ANOMALY` |
| `severity` | 3 / 2 / 2 / 1 |
| `url` | Deep link straight to that condition's buy page |
| `image` | Thumbnail URL |
| `ladder_gap` | Largest adjacent-tier gap, % (~3% normal) |
| `previous_price` | Price at the previous scan, when known |

`event.to_dict()` gives a JSON-safe dict for logging or queueing.

### Standalone, without a bot

Set `discord_webhook` in the config (or the `DISCORD_WEBHOOK_URL` environment
variable, which overrides it) and run `python run.py watch`.

---

## 4. Configuration

All keys optional; defaults come from `DEFAULT_CONFIG` in `openbox_monitor/runner.py`.

| Key | Default | Notes |
|---|---|---|
| `database` | `openbox.db` | SQLite path |
| `discord_webhook` | `""` | Standalone mode only |
| `discord_mention` | `""` | e.g. `"@here"`, prepended to webhook posts |
| `zip_code` | `""` | Destination zip. **Affects shipping eligibility** — set it |
| `requests_per_second` | `3.0` | Global token bucket. Do not raise much |
| `workers` | `6` | Scan threads |
| `cycle_seconds` | `60` | Pause between passes |
| `max_skus_per_cycle` | `400` | Caps a pass so the loop stays responsive |
| `alert_cooldown_seconds` | `21600` | Suppress repeats of the same sku+condition+price (6h) |
| `discovery_interval_seconds` | `3600` | Watchlist refresh cadence |
| `discovery_pages_per_source` | `3` | Listing pages to page through |
| `discovery_sources` | `[]` | Empty means `discovery.DEFAULT_SOURCES` |

### Polling tiers

SKUs are polled at a rate matched to their value, so expensive items get checked
often without the cheap tail eating the request budget.

```json
"intervals": { "hot": 120, "warm": 600, "cold": 1800,
               "hot_floor": 1500, "warm_floor": 600 }
```

New price `>= hot_floor` polls every `hot` seconds, `>= warm_floor` every
`warm`, everything else every `cold`. SKUs with no known price yet are treated
as hot so they get an initial scan.

### Thresholds

```json
"thresholds": {
  "alert_pct": 35.0, "strong_pct": 45.0, "ladder_gap_pct": 8.0,
  "price_drop_pct": 10.0, "min_new_price": 400.0, "min_dollars_off": 150.0
}
```

- `strong_pct` — always alerts, no state change needed. The real events sit at 45-62%.
- `alert_pct` — needs a transition (new offer, price drop, or detached ladder).
- `ladder_gap_pct` — tier spread marking a detached ladder. **Do not raise above ~8%**; normal is ~3% and the whole early-warning value lives in this gap.
- `min_dollars_off` — kills percentage noise on cheap items.

For a first run, lower `alert_pct` to ~15 for a day to confirm alerts flow, then
put it back. At production thresholds you may legitimately see nothing for days.

---

## 5. CLI

| Command | Does |
|---|---|
| `python run.py check <sku>` | Full open-box state for one SKU, all tiers confirmed |
| `python run.py discover` | Build/refresh the watchlist, exit |
| `python run.py scan [--post]` | One pass; `--post` sends to the webhook |
| `python run.py watch` | Run continuously |

`-c/--config` picks a config file, `-v` enables debug logging (shows every
backoff, which is what you want when diagnosing throttling).

---

## 6. How it works

Two stages per SKU, because HTTP requests are the scarce resource.

**Stage 1 — sweep (1 request per SKU).**
`GET /site/-/{sku}.p?skuId={sku}` returns the new price, every open-box tier,
each tier's price, and the product id. Buildable from the SKU alone. Cheap
enough to run across thousands of SKUs.

**Stage 2 — confirm (1 request per candidate tier).**
The base page renders fulfillment for the *selected* condition only, so a tier's
real status needs its own page:
`GET /product/a/{productId}/sku/{sku}/openbox?condition=good`.
Only tiers whose price already looks alertable get this request, so in steady
state stage 2 almost never runs.

The shipping gate is `fulfillmentOptions.buttonStates[]`, whose entries are
tagged with the condition they describe:

```json
{"buttonState":"ADD_TO_CART",  "condition":"OPEN_BOX_EXCELLENT"}   ships
{"buttonState":"CHECK_STORES", "condition":"OPEN_BOX_GOOD"}        pickup only
{"buttonState":"SOLD_OUT",     "condition":"OPEN_BOX_FAIR"}        gone
```

Only `ADD_TO_CART` becomes an event.

### Modules

| File | Role |
|---|---|
| `client.py` | TLS-impersonating HTTP, token bucket, retry/backoff, cooldown |
| `parser.py` | Page -> `Snapshot` of `Offer`s; ladder-gap maths |
| `discovery.py` | Harvest SKUs from listing pages |
| `store.py` | SQLite: watchlist, last-seen offer state, alert log |
| `detect.py` | Thresholds and the transition rules; emits `DealEvent` |
| `notify.py` | Discord embed construction and webhook delivery |
| `runner.py` | `Monitor` — the two-stage loop |

---

## 7. Constraints that will bite you

These were established by probing the live site. Each one looks like an
improvement waiting to happen and is not.

**Only `impersonate="chrome"` works.** Plain `requests`/`curl` against
`www.bestbuy.com` is blackholed by Akamai — the TCP connection opens and then
hangs until timeout, with no error. `curl_cffi` with the rolling `chrome`
profile returns 200. Pinned builds (`chrome124`, `chrome131`) are **rejected**
with an HTTP/2 stream reset. Do not "improve" `IMPERSONATIONS` by rotating over
pinned profiles; it fails on a subset of SKUs in a way that looks like a parser
bug.

**HTTP/2 stream resets are the rate limit.** Best Buy throttles bursts with
`curl: (92) ... INTERNAL_ERROR` rather than HTTP 429. `client.py` treats that as
a rate-limit signal, enters a shared cooldown across all threads, and rebuilds
the session. Raising `requests_per_second` much past 3 just trips this.

**A tier existing is not a tier being buyable.** Pages list prices for tiers
that are `SOLD_OUT`. Gate on `buttonState` or you will alert on phantom stock.

**The base page only renders fulfillment for the selected condition.** Other
tiers come back unconfirmed (`button_state is None`), which is why stage 2
exists. Never infer availability from the base page.

**Listing pages are only good for SKU harvesting.** The open-box category page
carries 135 SKUs but server-renders full `buyingOptions` for only the first
~4; the rest load client-side. Open-box state must come from the per-SKU scan.

**Condition URLs need the product id; the slug does not matter.**
`/product/a/{productId}/sku/{sku}/openbox?condition=fair` works — `a` is a
throwaway slug — but omitting the product id gives a 404. The id is parsed out
of the base page and cached in `products.product_id`.

**`zip_code` changes results.** Best Buy defaults to a Minneapolis zip
(`55423`). Shipping eligibility is destination-dependent, so set it.

---

## 8. Operational notes

- **Request budget.** A pass costs roughly one request per due SKU plus a few
  confirms. At `requests_per_second: 3` a 400-SKU pass takes about 2.5 minutes,
  so `cycle_seconds: 60` with `max_skus_per_cycle: 400` self-paces into a
  continuous rolling scan. That is intended.
- **State.** Everything lives in the SQLite file. Deleting it re-triggers
  `NEW_OFFER` for every currently-live offer, so expect one noisy pass after a
  wipe. The watchlist rebuilds itself via `discover`.
- **De-duplication** is keyed on sku + condition + exact price within
  `alert_cooldown_seconds`. A genuine price change re-alerts; a flapping scan
  does not.
- **Tuning the watchlist.** Edit `discovery_sources` in the config, or
  `DEFAULT_SOURCES` in `discovery.py`. Any Best Buy search or category URL
  works; SKUs are harvested by regex from the embedded payload.
- **Expected volume.** At defaults, most days produce zero alerts. If it is
  chattering, raise `min_dollars_off` first — it suppresses percentage noise on
  cheap items without touching sensitivity on expensive ones.
- **Scope.** Best Buy only. Micro Center open-box is in-store pickup only and
  cannot ship, so it is deliberately not implemented.
