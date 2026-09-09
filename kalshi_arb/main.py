"""
Kalshi <-> Polymarket cross-venue spread scanner.

Commands
--------
  check-config   show effective configuration and safety state
  doctor         check dependencies and reach both venue APIs
  suggest        rank cross-venue title matches for human review
  pairs          show the registry and what still needs verification
  verify         walk one pair's resolution-equivalence review
  reject         mark a pair as non-equivalent
  refresh        re-pull market metadata and CLOB token ids
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
from metadata import enrich_pair
from notify import ConsoleChannel, MacNotificationChannel, Notifier, TelegramChannel
from pairing import RiskFlag
from review import render_pair, run_review
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


async def _doctor(_args: argparse.Namespace) -> int:
    """
    Preflight. Checks the things that make the difference between a service
    that runs and one that crash-loops, and does it before you install it.
    """
    ok = True

    print(f"python              {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        print("  FAIL: Python 3.11+ required")
        ok = False

    for mod in ("aiohttp", "cryptography", "dotenv"):
        try:
            __import__(mod)
            print(f"import {mod:<13} ok")
        except ImportError:
            print(f"import {mod:<13} MISSING — pip install -r requirements.txt")
            ok = False

    print()
    async with aiohttp.ClientSession() as session:
        kalshi, poly = build_clients(session)

        # Both venue APIs are exercised for real here. The response shapes
        # this scanner parses were never confirmed against the live services,
        # so this is the check that turns that assumption into a fact.
        try:
            page = await kalshi.get_markets(limit=1)
            markets = page.get("markets", [])
            print(f"kalshi /markets     ok ({len(markets)} returned)")
            if markets:
                ticker = markets[0].get("ticker", "")
                yes, no = await kalshi.get_books(ticker, depth=3)
                print(f"kalshi orderbook    ok ({ticker}: yes ask "
                      f"{yes.best_ask}, no ask {no.best_ask})")
        except Exception as exc:
            print(f"kalshi              FAIL: {type(exc).__name__}: {str(exc)[:160]}")
            ok = False

        try:
            pm = await poly.get_markets(limit=1)
            print(f"polymarket /markets ok ({len(pm)} returned)")
            if pm:
                from metadata import polymarket_tokens
                try:
                    yes_tok, no_tok = polymarket_tokens(pm[0])
                    book = await poly.get_book(yes_tok, "YES")
                    print(f"polymarket book     ok (best ask {book.best_ask})")
                except Exception as exc:
                    print(f"polymarket book     FAIL: {type(exc).__name__}: {str(exc)[:160]}")
                    ok = False
        except Exception as exc:
            print(f"polymarket          FAIL: {type(exc).__name__}: {str(exc)[:160]}")
            ok = False

        if kalshi.signer is not None:
            try:
                bal = await kalshi.get_balance()
                print(f"kalshi auth         ok (balance endpoint reachable: {bal})")
            except Exception as exc:
                print(f"kalshi auth         FAIL: {type(exc).__name__}: {str(exc)[:160]}")
                ok = False
        else:
            print("kalshi auth         skipped (no credentials — read-only is fine)")

    print()
    print("READY" if ok else "NOT READY — fix the failures above before installing the service")
    return 0 if ok else 1


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
    added = skipped = 0
    for c in found:
        pair, problems = c.to_pair()
        if pair.pair_id in registry:
            continue
        if problems:
            # A pair without token ids cannot be scanned, so recording it
            # would put a permanently dead entry in the review queue.
            skipped += 1
            logger.warning("Skipping %s: %s", pair.pair_id, "; ".join(problems))
            continue
        registry.add(pair)
        added += 1
        print(f"{c.score:.3f}  {c.kalshi_title}")
        print(f"         <-> {c.polymarket_title}")
        print(f"         id: {pair.pair_id}")

    if added and args.write:
        registry.save(config.PAIRS_PATH)
        print(f"\nWrote {added} new UNVERIFIED pairs to {config.PAIRS_PATH}")
        if skipped:
            print(f"({skipped} candidates skipped — unusable metadata; run with "
                  "--log-level DEBUG for detail)")
        print("Each needs review before it can trade:")
        print("  python main.py verify <pair_id> --reviewer <your name>")
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


def cmd_verify(args: argparse.Namespace) -> int:
    registry = PairRegistry.load(config.PAIRS_PATH)
    pair = registry.get(args.pair_id)
    if pair is None:
        print(f"No pair {args.pair_id!r}. Run 'pairs' to list them.")
        return 1
    if pair.verification.value == "VERIFIED":
        print(f"{pair.pair_id} is already VERIFIED by {pair.verified_by} "
              f"at {pair.verified_at}.")
        print("Re-verify with --force if a rulebook has changed.")
        if not args.force:
            return 0

    outcome = run_review(pair, args.reviewer)
    if outcome.aborted:
        return 1
    registry.save(config.PAIRS_PATH)
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    registry = PairRegistry.load(config.PAIRS_PATH)
    pair = registry.get(args.pair_id)
    if pair is None:
        print(f"No pair {args.pair_id!r}.")
        return 1
    pair.mark_rejected(args.reviewer, RiskFlag(args.flag), notes=args.notes)
    registry.save(config.PAIRS_PATH)
    print(f"{pair.pair_id} REJECTED on {args.flag}. It will never be traded.")
    return 0


async def _refresh(args: argparse.Namespace) -> int:
    """
    Re-pull metadata and CLOB token ids for registered pairs.

    Needed because a pair without token ids is silently unscannable, and
    because close times and rules text change — a pair verified against a
    rulebook that has since been edited is no longer verified in any
    meaningful sense.
    """
    registry = PairRegistry.load(config.PAIRS_PATH)
    pairs = list(registry)
    if not pairs:
        print("No pairs registered.")
        return 0

    updated = 0
    async with aiohttp.ClientSession() as session:
        kalshi, poly = build_clients(session)
        for pair in pairs:
            k_market = p_market = None
            try:
                k_market = (await kalshi.get_market(pair.kalshi.market_id)).get("market")
            except Exception as exc:
                logger.warning("%s: kalshi fetch failed: %s", pair.pair_id, exc)
            try:
                p_market = await poly.get_market(pair.polymarket.market_id)
            except Exception as exc:
                logger.warning("%s: polymarket fetch failed: %s", pair.pair_id, exc)

            problems = enrich_pair(pair, k_market, p_market)
            status = "ok" if not problems else "; ".join(problems)
            marker = "  " if not problems else "! "
            print(f"{marker}{pair.pair_id:<44} {status}")
            if not problems:
                updated += 1

    registry.save(config.PAIRS_PATH)
    print(f"\nRefreshed {updated}/{len(pairs)} pairs into {config.PAIRS_PATH}")
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
    sub.add_parser("doctor", help="Check dependencies and reach both venue APIs")
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

    v = sub.add_parser("verify", help="Walk one pair's resolution-equivalence review")
    v.add_argument("pair_id")
    v.add_argument("--reviewer", required=True, help="Your name — recorded on the pair")
    v.add_argument("--force", action="store_true", help="Re-verify an already-verified pair")

    r = sub.add_parser("reject", help="Mark a pair as non-equivalent")
    r.add_argument("pair_id")
    r.add_argument("--reviewer", required=True)
    r.add_argument("--flag", required=True, choices=[f.value for f in RiskFlag])
    r.add_argument("--notes", default="")

    sub.add_parser("refresh", help="Re-pull market metadata and CLOB token ids")
    sub.add_parser("status", help="Read the daemon heartbeat")

    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    if args.command in (None, "check-config"):
        return cmd_check_config(args)
    if args.command == "doctor":
        return asyncio.run(_doctor(args))
    if args.command == "pairs":
        return cmd_pairs(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "reject":
        return cmd_reject(args)
    if args.command == "refresh":
        return asyncio.run(_refresh(args))
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
