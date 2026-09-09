"""
Kalshi <-> Polymarket cross-venue spread scanner.

Commands
--------
  check-config   show effective configuration and safety state
  suggest        rank cross-venue title matches for human review
  pairs          show the registry and what still needs verification
  scan           price every registered pair and report live edge
  watch          run the scan continuously (for a always-on local host)
  status         read the daemon heartbeat

Read-only unless credentials are configured, and never places an order unless
DRY_RUN is explicitly disabled (see config.py).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

import aiohttp

import config
from candidates import suggest
from daemon import ScanDaemon, setup_rotating_logs
from notify import ConsoleChannel, MacNotificationChannel, Notifier, TelegramChannel
from fees import DEFAULT_KALSHI_FEES, DEFAULT_POLYMARKET_FEES, DEFAULTS_VERIFIED_ON
from kalshi_auth import KalshiAuthError, KalshiSigner
from kalshi_client import DEMO_BASE, PROD_BASE, KalshiClient
from money import fmt_usd
from pairing import PairRegistry, Verification
from polymarket_client import PolymarketClient
from rate_limit import WeightedLimiter
from scanner import Spread, scan_pair

logger = logging.getLogger("kalshi_arb")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s – %(message)s",
        stream=sys.stderr,
    )


def build_clients(session: aiohttp.ClientSession) -> tuple[KalshiClient, PolymarketClient]:
    signer = None
    if config.KALSHI_KEY_ID and config.KALSHI_PRIVATE_KEY_PATH:
        try:
            signer = KalshiSigner.from_file(
                config.KALSHI_KEY_ID, config.KALSHI_PRIVATE_KEY_PATH
            )
        except KalshiAuthError as exc:
            logger.warning("Kalshi signing unavailable (%s) — continuing read-only", exc)

    kalshi = KalshiClient(
        session=session,
        limiter=WeightedLimiter(
            capacity=config.KALSHI_BUCKET_CAPACITY,
            refill_per_second=config.KALSHI_REFILL_PER_SEC,
        ),
        signer=signer,
        base_url=DEMO_BASE if config.KALSHI_USE_DEMO else PROD_BASE,
        dry_run=config.DRY_RUN,
    )
    poly = PolymarketClient(
        session=session,
        limiter=WeightedLimiter(
            capacity=config.POLY_BUCKET_CAPACITY,
            refill_per_second=config.POLY_REFILL_PER_SEC,
        ),
    )
    return kalshi, poly


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def render_spread(s: Spread) -> str:
    tag = "TRADEABLE" if s.tradeable else "RESEARCH "
    ann = f"{s.annualised_roi:.1%}" if s.annualised_roi is not None else "n/a"
    lines = [
        f"[{tag}] {s.pair_id}  ({s.direction})",
        f"    size          {s.contracts} contracts",
        f"    kalshi vwap   {s.kalshi_price}   poly vwap {s.poly_price}",
        f"    gross edge    {fmt_usd(s.gross_edge_per_contract, 4)}/contract",
        f"    fees          {fmt_usd(s.fees_total)} total",
        f"    net edge      {fmt_usd(s.net_edge_per_contract, 4)}/contract",
        f"    capital       {fmt_usd(s.capital_required)}",
        f"    net profit    {fmt_usd(s.net_profit)}   ROI {s.roi:.2%}   annualised {ann}",
    ]
    if not s.tradeable:
        lines.append(f"    ** NOT TRADEABLE: {s.block_reason}")
    return "\n".join(lines)


def passes_thresholds(s: Spread) -> bool:
    return (
        s.contracts >= config.MIN_CONTRACTS
        and s.net_edge_per_contract >= config.MIN_NET_EDGE_PER_CONTRACT
    )


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------

def cmd_check_config(_args: argparse.Namespace) -> int:
    print(config.describe())
    print(f"Fee schedule        : {DEFAULTS_VERIFIED_ON}")
    registry = PairRegistry.load(config.PAIRS_PATH)
    print(f"Registered pairs    : {len(registry)} "
          f"({len(registry.tradeable())} tradeable, "
          f"{len(registry.needing_review())} awaiting review)")
    if not config.DRY_RUN:
        print("\n*** LIVE MODE — order mutations are ENABLED ***")
    return 0


def cmd_pairs(_args: argparse.Namespace) -> int:
    registry = PairRegistry.load(config.PAIRS_PATH)
    if not len(registry):
        print(f"No pairs registered at {config.PAIRS_PATH}.")
        print("Run 'suggest' to generate candidates for review.")
        return 0
    for pair in registry:
        status = pair.verification.value
        mark = "OK " if pair.is_tradeable else "-- "
        print(f"{mark}{pair.pair_id:<44} {status}")
        print(f"    kalshi     {pair.kalshi.market_id}: {pair.kalshi.title}")
        print(f"    polymarket {pair.polymarket.market_id}: {pair.polymarket.title}")
        if not pair.is_tradeable:
            print(f"    blocked    {pair.block_reason()}")
    return 0


async def _suggest(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession() as session:
        kalshi, poly = build_clients(session)
        logger.info("Fetching open markets from both venues…")
        k_markets, p_markets = await asyncio.gather(
            kalshi.iter_markets(status="open"),
            poly.iter_markets(),
        )
    logger.info("Kalshi: %d markets, Polymarket: %d markets", len(k_markets), len(p_markets))

    found = suggest(k_markets, p_markets, threshold=args.threshold, limit=args.limit)
    if not found:
        print("No candidate pairs above threshold.")
        return 0

    registry = PairRegistry.load(config.PAIRS_PATH)
    added = 0
    for c in found:
        pair = c.to_pair()
        if pair.pair_id in registry:
            continue
        registry.add(pair)
        added += 1
        print(f"{c.score:.3f}  {c.kalshi_title}")
        print(f"         <-> {c.polymarket_title}")

    if added and args.write:
        registry.save(config.PAIRS_PATH)
        print(f"\nWrote {added} new UNVERIFIED pairs to {config.PAIRS_PATH}")
        print("Each needs a human to read both rulebooks before it can trade.")
    elif added:
        print(f"\n{added} new candidates (re-run with --write to save them)")
    return 0


async def scan_once(session, pairs) -> list[Spread]:
    """
    One scan pass over `pairs`. Shared by the one-shot command and the daemon.

    A book-fetch failure on one pair is logged and skipped rather than raised:
    one delisted market must not blind the scanner to every other pair. A
    failure that affects every pair surfaces as an empty result and is handled
    by the daemon's backoff.
    """
    kalshi, poly = build_clients(session)
    results: list[Spread] = []
    for pair in pairs:
        if not (pair.polymarket_yes_token and pair.polymarket_no_token):
            logger.warning(
                "Skipping %s: Polymarket token ids not set on the pair", pair.pair_id
            )
            continue
        try:
            # Both venues fetched concurrently: pricing one leg seconds after
            # the other manufactures edge on a moving market.
            (k_yes, k_no), (p_yes, p_no) = await asyncio.gather(
                kalshi.get_books(pair.kalshi.market_id, depth=config.BOOK_DEPTH),
                poly.get_books(pair.polymarket_yes_token, pair.polymarket_no_token),
            )
        except Exception as exc:
            logger.error("Book fetch failed for %s: %s", pair.pair_id, exc)
            continue

        results.extend(scan_pair(
            pair,
            kalshi_yes=k_yes, kalshi_no=k_no, poly_yes=p_yes, poly_no=p_no,
            kalshi_fees=DEFAULT_KALSHI_FEES, poly_fees=DEFAULT_POLYMARKET_FEES,
        ))
    return [s for s in results if passes_thresholds(s)]


def load_pairs(include_unverified: bool):
    registry = PairRegistry.load(config.PAIRS_PATH)
    return list(registry) if include_unverified else registry.tradeable()


def build_notifier() -> Notifier:
    channels = [ConsoleChannel()]
    if config.MACOS_NOTIFICATIONS:
        channels.append(MacNotificationChannel())
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        channels.append(TelegramChannel(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID))
    return Notifier(
        channels,
        cooldown_sec=config.ALERT_COOLDOWN_SEC,
        improvement_threshold=config.ALERT_IMPROVEMENT,
        state_path=config.ALERT_STATE_PATH,
    )


async def _scan(args: argparse.Namespace) -> int:
    pairs = load_pairs(args.include_unverified)
    if not pairs:
        which = "registered" if args.include_unverified else "verified"
        print(f"No {which} pairs to scan. Run 'suggest' first, then verify them.")
        return 0

    async with aiohttp.ClientSession() as session:
        qualifying = await scan_once(session, pairs)
    qualifying.sort(key=lambda s: s.net_profit, reverse=True)

    if not qualifying:
        print(f"Scanned {len(pairs)} pairs — no spread cleared the thresholds "
              f"(min {fmt_usd(config.MIN_NET_EDGE_PER_CONTRACT, 4)}/contract, "
              f"min {config.MIN_CONTRACTS} contracts).")
        return 0

    print(f"Scanned {len(pairs)} pairs — {len(qualifying)} spreads above threshold\n")
    for s in qualifying:
        print(render_spread(s))
        print()

    tradeable = [s for s in qualifying if s.tradeable]
    print(f"{len(tradeable)} tradeable, {len(qualifying) - len(tradeable)} research-only")
    if tradeable:
        total = sum((s.net_profit for s in tradeable), Decimal(0))
        capital = sum((s.capital_required for s in tradeable), Decimal(0))
        print(f"Tradeable total: {fmt_usd(total)} profit on {fmt_usd(capital)} capital")
    return 0


async def _watch(args: argparse.Namespace) -> int:
    setup_rotating_logs(config.LOG_PATH, args.log_level)

    pairs = load_pairs(args.include_unverified)
    if not pairs:
        which = "registered" if args.include_unverified else "verified"
        logger.error("No %s pairs to watch. Run 'suggest', then verify them.", which)
        return 1

    notifier = build_notifier()
    alert_on_research = args.alert_on_research or config.ALERT_ON_RESEARCH

    def dispatch(spreads: list[Spread]) -> int:
        # Unverified pairs are priced for research, but waking someone at 3am
        # for a spread that cannot legally be traded yet is how an alert
        # channel gets muted. Opt in explicitly.
        eligible = spreads if alert_on_research else [s for s in spreads if s.tradeable]
        return len(notifier.notify(eligible, render=render_spread))

    async with aiohttp.ClientSession() as session:
        async def cycle() -> list[Spread]:
            return await scan_once(session, pairs)

        daemon = ScanDaemon(
            cycle,
            on_spreads=dispatch,
            interval_sec=args.interval or config.SCAN_INTERVAL_SEC,
            max_backoff_sec=config.MAX_BACKOFF_SEC,
            heartbeat_path=config.HEARTBEAT_PATH,
            max_cycles=args.max_cycles,
        )
        daemon.install_signal_handlers()
        logger.info(
            "Watching %d pairs (%s), alerting on %s",
            len(pairs),
            "including unverified" if args.include_unverified else "verified only",
            "all spreads" if alert_on_research else "tradeable only",
        )
        await daemon.run()
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    import json
    if not config.HEARTBEAT_PATH.exists():
        print(f"No heartbeat at {config.HEARTBEAT_PATH} — daemon has not run.")
        return 1
    stats = json.loads(config.HEARTBEAT_PATH.read_text())
    for key, value in stats.items():
        print(f"{key:<22} {value}")

    last = stats.get("last_cycle_at")
    if last:
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(last)).total_seconds()
        stale_after = config.SCAN_INTERVAL_SEC * 5
        print()
        if age > stale_after:
            print(f"STALE: last cycle {age:.0f}s ago (expected every "
                  f"{config.SCAN_INTERVAL_SEC:.0f}s) — daemon may be wedged or stopped.")
            return 1
        print(f"Healthy: last cycle {age:.0f}s ago.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi/Polymarket cross-venue spread scanner")
    parser.add_argument("--log-level", default=config.LOG_LEVEL,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check-config", help="Show configuration and safety state")
    sub.add_parser("pairs", help="List registered pairs and verification status")

    s = sub.add_parser("suggest", help="Find candidate pairs for human review")
    s.add_argument("--threshold", type=float, default=0.45)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--write", action="store_true", help="Save candidates to the registry")

    sc = sub.add_parser("scan", help="Price registered pairs")
    sc.add_argument("--include-unverified", action="store_true",
                    help="Also price pairs awaiting verification (reported as RESEARCH)")

    w = sub.add_parser("watch", help="Scan continuously (for an always-on host)")
    w.add_argument("--include-unverified", action="store_true",
                   help="Also price pairs awaiting verification")
    w.add_argument("--alert-on-research", action="store_true",
                   help="Alert on unverified pairs too (default: tradeable only)")
    w.add_argument("--interval", type=float, default=None,
                   help=f"Seconds between cycles (default {config.SCAN_INTERVAL_SEC:.0f})")
    w.add_argument("--max-cycles", type=int, default=None,
                   help="Stop after N cycles (for testing)")

    sub.add_parser("status", help="Read the daemon heartbeat")

    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    if args.command in (None, "check-config"):
        return cmd_check_config(args)
    if args.command == "pairs":
        return cmd_pairs(args)
    if args.command == "suggest":
        return asyncio.run(_suggest(args))
    if args.command == "scan":
        return asyncio.run(_scan(args))
    if args.command == "watch":
        return asyncio.run(_watch(args))
    if args.command == "status":
        return cmd_status(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
