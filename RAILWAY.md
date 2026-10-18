# Railway deployment

Run this project as one continuously running background worker. It posts to
the chosen Discord channel using an incoming webhook; no Discord bot token,
public domain, HTTP server, or port is needed.

## Service settings

The GitHub repository root contains `run.py` and `requirements.txt`.
Use these settings on the Bestbuy-Unbox-Monitor service:

| Setting | Value |
| --- | --- |
| Source | `csdingzhen/Bestbuy-Unbox-Monitor`, branch `main` |
| Root directory | `/` (repository root) |
| Builder | Railpack |
| Custom build command | Leave unset; Railpack installs `requirements.txt` |
| Start command | `python -u run.py -c config.railway.json watch` |
| Replicas | 1 |
| Serverless / sleep | Disabled |
| Cron schedule | Unset; `watch` schedules scans itself |
| HTTP healthcheck | Unset; this process has no HTTP endpoint |
| Restart policy | On failure, with retries enabled |
| Persistent volume mount | `/app/data` |

The `-u` flag makes printed output appear promptly in Railway logs. Discovery
runs automatically on startup; do not put `discover` in a pre-deploy command.
Railway volumes are mounted at runtime, not during builds/pre-deploy commands.

## Variables

Set these on the service's Variables tab:

```text
DISCORD_WEBHOOK_URL=<incoming webhook URL for your target Discord channel>
BESTBUY_ZIP_CODE=<your five-digit destination ZIP>
OPENBOX_DATABASE=/app/data/openbox.db
```

Create the Discord webhook in the target channel's settings under
Integrations > Webhooks, then copy its URL directly into Railway. Treat it
as a secret. `DISCORD_MENTION` is optional, for example `@here`.

These environment values override the JSON file. The SQLite database stores
the watchlist, previous offers, and alert suppression history. Mount the volume
and set the database variable before starting the worker; otherwise redeploys
can lose that history. Use only one worker against this database.

## Publish and verify

1. Commit and push the local configuration/code changes to the connected
   repository after reviewing them. A push to `main` can trigger Railway's
   automatic deployment, so configure the volume and variables first.
2. Apply the service settings above and deploy the updated revision.
3. Check build logs for dependency installation, then runtime logs for
   `watching ... SKUs`, discovery counts, and `scanning ... SKUs`.
   `watching 0 SKUs` is normal before the first discovery finishes.
4. Zero discovered SKUs, repeated fetch failures, or absent parsed prices
   require investigation even when Railway labels the process running.
   Best Buy access and page parsing must be verified from Railway's network.
5. For one known laptop SKU, run
   `python run.py -c config.railway.json check <sku>` from the service's shell.
   Confirm that name, new price, condition prices, and availability parse.
   This command does not send a notification.
6. Verify Discord delivery with an explicitly chosen qualifying offer using
   `scan --post`, or a separately authorized test message. An ordinary `scan`
   also records alert suppression entries, so use a separate temporary
   database for diagnostic scans if you want to preserve production alerts.

## Detection scope and remaining limitations

- `config.railway.json` uses laptop-oriented sources and starts at 2 requests
  per second / 4 threads. It is a monitor profile, not Railway's infrastructure
  configuration format. The generic defaults also search GPUs, desktops,
  and monitors. Source selection is not a strict product-category filter:
  the regex harvester can collect other products embedded in listing pages.
- A newly published offer is detectable only after its SKU is discovered and
  scanned. Discovery runs hourly across the first three pages per source;
  this does not cover all Best Buy inventory or guarantee immediate alerts.
- Defaults require a new price of at least $400 and savings of at least $150.
  At 45% off the live new price, a buyable offer qualifies directly. At 35%
  off, it needs a new offer, a 10% price drop since the last scan, or an 8%
  condition-price gap. A newly buyable tier with an 8% gap can also qualify
  below 35%. These are heuristics, not evidence of a historical price anomaly.
- The database retains the latest offer state, not a historical price series.
  It cannot establish that a price is below a 7-day or 30-day average.
- Shipping is inferred from the parsed `ADD_TO_CART` condition state. The
  parser does not currently require an explicit shipping-eligibility flag.
  Confirm destination-specific shipping manually during the initial check.
- Alert suppression is recorded before Discord confirms delivery. Failed
  delivery has no durable retry queue, and can suppress the same-price alert
  for six hours. Strong discounts can re-alert after that cooldown even if
  unchanged. A durable outbox/acknowledgement flow is the next reliability fix.
- Best Buy's markup and datacenter access can change. Local/offline checks
  do not establish that collection or Discord delivery works on Railway.

References: [Railpack](https://docs.railway.com/builds/railpack),
[Volumes](https://docs.railway.com/volumes),
[Serverless](https://docs.railway.com/deployments/serverless).
