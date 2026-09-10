# Kalshi ↔ Polymarket cross-venue scanner

Finds and prices binary-contract spreads between Kalshi and Polymarket.

Buy YES on the cheap venue and NO on the other. If both markets resolve
identically, exactly one leg pays $1 per contract and the position is riskless:

```
profit_per_contract = 1 - price_yes - price_no - fees
```

## The thing that decides whether this works

If the two markets resolve **differently**, both legs lose and you lose the
entire capital deployed — not the edge, the capital. A 2-cent edge against a
98-cent downside needs resolution criteria to match on well over 99% of pairs
just to break even.

Two markets can look identical and resolve differently for entirely mundane
reasons: a different data source, a different observation time, different
rounding, different handling of revisions, different edge-case wording for
postponements and withdrawals. None of that is visible in the market title.

So this scanner **does not fuzzy-match titles into trades**. Pairs are curated
and must carry an explicit human verification before anything is marked
tradeable. `suggest` proposes candidates; a person reads both rulebooks. The
`candidates` module scores "Will the Fed cut rates in December?" against "Will
the Fed **raise** rates in December?" at 0.67 — which is exactly why title
similarity can never be the deciding step.

Unverified pairs are still priced, and reported as `RESEARCH`. You want to know
whether an edge exists before spending an hour on two rulebooks.

## Safety

- `DRY_RUN` is the default and is **opt-out via an exact sentinel string**, so
  a stray `1` or `true` cannot enable live trading.
- The gate lives in the client, not the caller, so there is no code path to a
  live order that bypasses it.
- Polymarket support is read-only. Nothing here can move funds on-chain.
- Order mutations require a `client_order_id`, so a retry after an ambiguous
  network failure is idempotent rather than doubling the position.

## Usage

```bash
python main.py check-config   # config and safety state
python main.py doctor         # deps + reach both venue APIs
python main.py suggest        # propose candidate pairs (--write to save)
python main.py pairs          # registry and verification status
python main.py verify <id>    # walk one pair's equivalence review
python main.py reject <id>    # mark a pair non-equivalent
python main.py refresh        # re-pull metadata and CLOB token ids
python main.py scan           # price verified pairs once
python main.py watch          # scan continuously
python main.py status         # read the daemon heartbeat
```

## Getting from a clone to a running service

Do these in order. Steps 3 and 4 are the ones that cannot be skipped or
automated — everything else is setup.

```bash
# 1. Environment (macOS system Python is 3.9; you need 3.11+)
brew install python@3.12
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Preflight. Hits both venue APIs for real and prints READY / NOT READY.
python main.py doctor
```

`doctor` is not a formality. The response shapes this scanner parses were
never confirmed against the live services — the build environment could not
reach either host — so this is the step that turns that assumption into a
fact. If it prints NOT READY, the parsing needs fixing before anything else
is worth doing.

```bash
# 3. Generate candidate pairs. Read-only; nothing is tradeable yet.
python main.py suggest --write
python main.py pairs
```

```bash
# 4. Review each pair. This is the human step the strategy rests on.
python main.py verify <pair_id> --reviewer "your name"
```

`verify` prints both venues' resolution terms side by side, then asks about
each divergence class in turn. It is built so the careless path is harder
than the careful one: the default answer to every question is the blocking
one, `same` must be typed in full for each of the six classes, answering
`differ` on any blocking class rejects the pair immediately and stops, and an
interrupted review records nothing rather than leaving a pair looking
reviewed. Who verified and when is stored on the pair.

Read the actual rulebooks. The interview cannot check anything for you — it
only makes sure you were asked.

```bash
# 5. Confirm it prices them, then install the service.
python main.py scan
deploy/install-macos.sh
```

## Running 24/7 on a Mac mini

`watch` is the long-running form. It never places orders — it is a monitor.
Execution is a separate decision with a separate risk profile, and wiring it
into an unattended loop is not something to do implicitly.

```bash
python -m pip install -r requirements.txt
deploy/install-macos.sh
```

The installer validates dependencies and configuration **before** installing,
because a service that installs cleanly and then crash-loops on a missing
import is harder to debug than one that refuses to install. It registers a
launchd *user agent* (no root — the scanner does not need it, and running
network-facing code as root for no reason is a poor trade).

```bash
python main.py status                      # health + last cycle age
tail -f data/scanner.log                   # live log
launchctl unload -w ~/Library/LaunchAgents/com.kalshiarb.scanner.plist   # stop
deploy/uninstall-macos.sh                  # remove
```

Two things a headless 24/7 Mac mini needs beyond the service itself:

```bash
sudo pmset -a sleep 0 disksleep 0    # a sleeping Mac stops scanning silently
```

and auto-login enabled (System Settings → Users & Groups → Automatic login),
since a *user* agent only runs while the user is logged in. If you would
rather not enable auto-login, move the plist to `/Library/LaunchDaemons` and
add a `UserName` key — but then it runs as root, which is worth avoiding.

### What makes the loop survive unattended

Each of these exists because of a specific way a long-running loop fails:

| Property | Failure it prevents |
|---|---|
| SIGTERM finishes the cycle then exits | launchd SIGKILLs after a grace period; a loop that sleeps its full interval gets killed mid-request |
| Exponential backoff to a 15-min cap | a venue outage otherwise retries every 60s, burning rate-limit budget and burying the real error |
| Heartbeat file | a process can be alive and wedged; `status` reports the last *completed* cycle |
| Rotating logs (10MB × 5) | weeks of 60-second cycles fill a disk |
| `ThrottleInterval` 60 in the plist | a config error that crashes at startup otherwise becomes a hot restart loop |
| Alert suppression | see below — the reason a 24/7 scanner stays worth reading |

## How you know it is doing anything

Spread alerts are silent by design when there is nothing to report, so silence
on its own carries no information: a scanner finding nothing looks exactly like
one that died on Tuesday. Three mechanisms close that from different
directions.

**Spread alerts** — when a tradeable spread clears the thresholds, suppressed
as described below.

**Outage alerts** — after `FAILURE_ALERT_THRESHOLD` consecutive failed cycles
(default 3, so about three minutes at the default interval), you get told,
with the error and where to look. Long enough not to fire on a transient blip;
short enough to hear about a real outage quickly. While the outage continues
the alert repeats only on a cooldown, so twelve hours down is a handful of
messages rather than 720. Recovery is reported once, so an outage always has a
visible end.

**A liveness digest** — every `DIGEST_INTERVAL_SEC` (default 24h): cycles run,
failures, spreads found, best edge seen, current status. It sends *whether or
not anything happened*. That is the entire point — a digest that only reported
interesting news would reinstate the ambiguity it exists to remove. Once it is
running, **the digest's absence is the alarm**.

Digest timing is persisted, so a process restarted more often than the digest
interval still emits one. Without that the failure would hide precisely when
restarts are frequent, which is when you most want to hear from it.

On demand, any time:

```bash
python main.py status     # cycles, failures, last-cycle age, staleness verdict
tail -f data/scanner.log
```

`status` reports STALE if the last completed cycle is older than 5× the scan
interval — a process can be alive and wedged, and that is what catches it.

### Alerts that reach you when you are not at the machine

This matters more than it sounds for a headless Mac mini. Console output and
macOS banners only help someone sitting at the machine. **Telegram is the only
channel that finds you anywhere else**, and `check-config` warns when none is
set.

Setting it up takes about two minutes:

1. In Telegram, message [@BotFather](https://t.me/botfather), send
   `/newbot`, and follow the prompts. It replies with a token like
   `123456789:AAE...`.
2. Send your new bot any message (a bot cannot start a conversation with you).
3. Get your chat id:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" \
     | python3 -c "import sys,json; print(json.load(sys.stdin)['result'][0]['message']['chat']['id'])"
   ```
4. Put both in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AAE...
   TELEGRAM_CHAT_ID=987654321
   ```
5. Confirm: `python main.py check-config` should list `telegram` under alert
   channels and drop the headless warning.

### Alert suppression

A spread that persists for six hours is **one** opportunity. At a 60-second
interval a naive notifier reports it 360 times, and the practical result is
that you stop reading alerts — which costs you the one that mattered.

An alert fires only when there is new information: the pair has not alerted
inside the cooldown (default 1h), **or** its net edge per contract improved by
at least `ALERT_IMPROVEMENT` (default 1c). Widening counts because it changes
the sizing decision. The tracker keeps the *best* edge seen rather than the
latest, so a spread oscillating around one level does not re-alert on every
upswing. Suppression state persists across restarts.

Channels are independent and best-effort: console always, macOS banners by
default (inert off-platform), Telegram if configured. A channel failure is
logged, never raised — a Telegram outage must not stop the loop.

## Numeric approach

Prices are fixed-point on a centi-cent grid, never float. A cross-venue spread
is a difference of two nearly equal numbers — the arithmetic where float error
stops being academic. Kalshi quotes integer cents and whole contracts;
Polymarket quotes decimals and fractional shares; `money.py` is the single
conversion point onto one canonical scale.

Fees are applied before anything is called actionable. Kalshi's fee at 50c is
1.75c per contract, which is frequently **larger** than the raw spread — a
scanner reporting gross edge would hand you a list of trades that all lose
money.

## Sizing

Quoting edge off the top of book and then sizing into it is the standard way to
report profit that doesn't exist. `scanner.py` walks both ask ladders in
lockstep and stops at the first level pair where the marginal contract stops
being profitable, so the reported size is the size that actually clears at the
reported edge.

Fills are all-or-nothing per leg: a partial fill on one side is not a smaller
arbitrage, it is an unhedged directional position.

## What is not modelled

- **Legging risk.** Two venues, two latencies; between filling leg one and leg
  two the second can move. This is the main live-execution risk.
- **Capital transfer time** between venues.
- **Settlement lag** — the two venues can free capital days apart, which is a
  funding cost even when both resolve the same way.

## Verification status

Pure logic (money, fees, order books, spread math, sizing, the resolution
gate, the rate limiter, request signing) is covered by the test suite:

```bash
python -m pytest tests/ -q
```

The REST clients' **wire formats are unverified against the live APIs** — both
hosts were unreachable from the environment this was built in. Response parsing
and retry policy are tested against recorded shapes; treat the first live run
as a smoke test. Fee coefficients and rate-limit weights are configurable and
their bundled defaults are marked unverified: check both against the venues'
current published schedules before sizing anything real.
