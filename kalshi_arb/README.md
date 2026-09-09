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
pip install -r requirements.txt
cp .env.example .env          # optional; read-only scanning needs nothing

python main.py check-config              # show config and safety state
python main.py suggest --write           # propose candidate pairs
python main.py pairs                     # review verification status
python main.py scan                      # price verified pairs
python main.py scan --include-unverified # include RESEARCH pairs
```

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
