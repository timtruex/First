"""Command line entry point. Everything the bot does is available here without Telegram."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config, resolve_encoder
from .db import JobStore
from .plan import script_preview
from .pipeline import load_draft, run_render_stage, run_script_stage
from .sources import extract_asin, resolve_short_link

log = logging.getLogger("amzvid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amzvid", description="Amazon review video pipeline")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="create a job from an affiliate link")
    p_new.add_argument("url")
    p_new.add_argument("--images", type=Path, help="directory or file(s) of product photos")
    p_new.add_argument("--title", default="")
    p_new.add_argument("--price", default="")
    p_new.add_argument("--notes", default="", help="your research notes; the script leans on these")
    p_new.add_argument("--bullet", action="append", default=[], help="repeatable listing bullet")

    p_script = sub.add_parser("script", help="write the script for a job")
    p_script.add_argument("job_id")

    p_render = sub.add_parser("render", help="voice and render an approved job")
    p_render.add_argument("job_id")

    p_run = sub.add_parser("run", help="new + script + render in one go")
    p_run.add_argument("url")
    p_run.add_argument("--images", type=Path)
    p_run.add_argument("--title", default="")
    p_run.add_argument("--price", default="")
    p_run.add_argument("--notes", default="")
    p_run.add_argument("--bullet", action="append", default=[])

    p_paste = sub.add_parser(
        "paste", help="create a job from a bookmarklet capture (clipboard by default)"
    )
    p_paste.add_argument("--file", type=Path, help="read the capture from a file instead")
    p_paste.add_argument("--notes", default="", help="your research notes; the script leans on these")
    p_paste.add_argument("--go", action="store_true", help="also write the script and render")

    p_watch = sub.add_parser(
        "watch", help="sit running and pick up each bookmarklet capture automatically"
    )
    p_watch.add_argument(
        "--no-prompt", action="store_true", help="do not ask for notes; render immediately"
    )
    p_watch.add_argument("--interval", type=float, default=1.0, help="clipboard poll seconds")

    p_jobs = sub.add_parser("jobs", help="list recent jobs")
    p_jobs.add_argument("--limit", type=int, default=15)

    sub.add_parser("bot", help="run the Telegram bot")
    sub.add_parser("doctor", help="check the local setup")
    sub.add_parser("youtube-auth", help="authorize YouTube uploads (opens a browser, once)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        # `doctor` exists to report problems, so it must survive a broken config.
        if args.command == "doctor":
            return _doctor_without_config(exc)
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    store = JobStore(cfg.paths.db)
    handlers = {
        "new": lambda: _cmd_new(cfg, store, args),
        "script": lambda: _cmd_script(cfg, store, args.job_id),
        "render": lambda: _cmd_render(cfg, store, args.job_id),
        "run": lambda: _cmd_run(cfg, store, args),
        "paste": lambda: _cmd_paste(cfg, store, args),
        "watch": lambda: _cmd_watch(cfg, store, args),
        "jobs": lambda: _cmd_jobs(store, args.limit),
        "bot": lambda: _cmd_bot(cfg, store),
        "doctor": lambda: _cmd_doctor(cfg),
        "youtube-auth": lambda: _cmd_youtube_auth(cfg),
    }
    try:
        return handlers[args.command]()
    except KeyboardInterrupt:
        return 130
    except (ConfigError, FileNotFoundError, RuntimeError, ValueError) as exc:
        log.error("%s", exc)
        return 1


# --------------------------------------------------------------------------- #

def _cmd_new(cfg: Config, store: JobStore, args) -> int:
    job = store.create(args.url, extract_asin(resolve_short_link(args.url)), chat_id=None)
    job_dir = cfg.paths.job_dir(job.id)
    inbox = job_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    if args.images:
        copied = _stage_images(args.images, inbox)
        log.info("staged %d image(s)", copied)

    import json

    meta = {
        "title": args.title,
        "price": args.price,
        "notes": args.notes,
        "bullets": args.bullet,
    }
    (job_dir / "product.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    store.set_state(job.id, "queued", "created via cli")
    print(job.id)
    return 0


def _cmd_script(cfg: Config, store: JobStore, job_id: str) -> int:
    job = store.get(job_id)
    store.set_state(job_id, "scripting")
    try:
        _product, draft = run_script_stage(cfg, job_id, job.affiliate_url)
    except Exception as exc:
        store.fail(job_id, str(exc))
        raise
    store.set_state(job_id, "awaiting_approval")
    print(script_preview(draft))
    return 0


def _cmd_render(cfg: Config, store: JobStore, job_id: str) -> int:
    job = store.get(job_id)
    job_dir = cfg.paths.job_dir(job_id)
    if not (job_dir / "script.json").exists():
        log.error("no script for %s yet - run: amzvid script %s", job_id, job_id)
        return 1

    from .sources import get_source

    store.set_state(job_id, "rendering")
    try:
        product = get_source(cfg).fetch(job.affiliate_url, job_dir)
        draft = load_draft(cfg, job_id)
        results = run_render_stage(cfg, job_id, product, draft)
    except Exception as exc:
        store.fail(job_id, str(exc))
        raise
    store.set_state(job_id, "done")

    print(f"\n{len(results)} file(s) in {cfg.paths.out_dir(job_id)}:")
    for item in results:
        print(f"  {item.kind:5s} {item.duration_s:6.1f}s  {item.path.name}  {item.title}")
    return 0


def _cmd_run(cfg: Config, store: JobStore, args) -> int:
    if (rc := _cmd_new(cfg, store, args)) != 0:
        return rc
    job = store.list_jobs(limit=1)[0]
    if (rc := _cmd_script(cfg, store, job.id)) != 0:
        return rc
    return _cmd_render(cfg, store, job.id)


def _cmd_paste(cfg: Config, store: JobStore, args) -> int:
    from .sources.clipboard import PayloadError, ingest, parse_payload

    raw, origin = _read_capture(args.file)
    if not raw.strip():
        log.error(
            "%s is empty. Open the Amazon listing, scroll the photo gallery once, then "
            "click the amzvid Capture bookmarklet and wait for the green banner.",
            origin,
        )
        return 1

    from .sources.clipboard import looks_like_payload

    if not looks_like_payload(raw):
        log.error(
            "%s does not hold an amzvid capture. It starts with:\n    %s",
            origin, _preview(raw),
        )
        log.error(
            "If you saw no green banner on the listing page, the bookmarklet did not "
            "run. If you saw one, something else has overwritten the clipboard since."
        )
        return 1

    try:
        payload = parse_payload(raw)
    except PayloadError as exc:
        log.error("%s", exc)
        return 1

    job = store.create(payload["url"], payload["asin"], chat_id=None)
    count = ingest(cfg, payload, cfg.paths.job_dir(job.id), notes=args.notes)
    if not count:
        store.fail(job.id, "no images could be downloaded")
        log.error("could not download any of the %d image(s)", len(payload["images"]))
        return 1

    store.set_state(job.id, "queued", "created from a bookmarklet capture")
    log.info(
        "%s: %s - %d image(s), %d bullet(s)",
        job.id, payload["title"][:60], count, len(payload["bullets"]),
    )
    if not args.go:
        print(job.id)
        return 0
    if (rc := _cmd_script(cfg, store, job.id)) != 0:
        return rc
    return _cmd_render(cfg, store, job.id)


def _cmd_watch(cfg: Config, store: JobStore, args) -> int:
    """Poll the clipboard and process each new capture.

    This exists because the clipboard is a single slot: any workflow that asks the
    operator to copy a terminal command between clicking the bookmarklet and running it
    overwrites the capture with the command. Here nothing is copied in between - the
    watcher is already running when the bookmarklet is clicked.
    """
    import hashlib
    import time

    from .sources.clipboard import PayloadError, looks_like_payload, parse_payload

    if not shutil.which("pbpaste"):
        log.error("watch needs pbpaste (macOS). Use `amzvid paste --file` elsewhere.")
        return 1

    seen: set[str] = set()
    log.info("watching the clipboard. Click the bookmarklet on a listing; ctrl-c to stop.")

    try:
        while True:
            raw = _read_clipboard()
            if looks_like_payload(raw):
                digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
                if digest not in seen:
                    seen.add(digest)
                    # Re-read the config per capture so tuning a setting takes effect on
                    # the next click instead of needing the watcher restarted.
                    _process_capture(_reload(cfg, args), store, raw, args)
                    log.info("watching again; click the bookmarklet for the next product.")
            time.sleep(max(0.25, args.interval))
    except KeyboardInterrupt:
        print()
        log.info("stopped after %d capture(s).", len(seen))
        return 0


def _reload(current: Config, args) -> Config:
    """Pick up config edits between captures; keep the old config if the file is broken."""
    try:
        fresh = load_config(args.config)
    except (ConfigError, OSError) as exc:
        log.warning("config has a problem, using the previously loaded one: %s", exc)
        return current
    if fresh != current:
        log.info("config reloaded")
    return fresh


def _process_capture(cfg: Config, store: JobStore, raw: str, args) -> None:
    from .sources.clipboard import PayloadError, ingest, parse_payload

    try:
        payload = parse_payload(raw)
    except PayloadError as exc:
        log.error("ignoring that capture: %s", exc)
        return

    log.info("captured %s - %s", payload["asin"], payload["title"][:70])
    notes = ""
    if not args.no_prompt:
        try:
            notes = input("  notes (enter to skip): ").strip()
        except EOFError:
            notes = ""

    job = store.create(payload["url"], payload["asin"], chat_id=None)
    try:
        count = ingest(cfg, payload, cfg.paths.job_dir(job.id), notes=notes)
    except Exception as exc:
        log.exception("could not fetch photos for %s", job.id)
        store.fail(job.id, str(exc))
        return
    if not count:
        store.fail(job.id, "no images could be downloaded")
        log.error("none of the %d image URL(s) downloaded", len(payload["images"]))
        return

    log.info("%s: %d photo(s), %d bullet(s)", job.id, count, len(payload["bullets"]))
    store.set_state(job.id, "queued", "created from a clipboard capture")
    if _cmd_script(cfg, store, job.id) == 0:
        _cmd_render(cfg, store, job.id)


def _read_clipboard() -> str:
    """Always the clipboard. Never stdin: a poll loop cannot re-read a stream."""
    import subprocess

    pbpaste = shutil.which("pbpaste")
    if not pbpaste:
        return ""
    try:
        return subprocess.run([pbpaste], capture_output=True, text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _read_capture(path: Path | None) -> tuple[str, str]:
    """A file, then stdin when piped, then the macOS clipboard. Returns (text, origin)."""
    if path:
        return path.read_text(), str(path)
    if not sys.stdin.isatty():
        return sys.stdin.read(), "stdin"
    pbpaste = shutil.which("pbpaste")
    if not pbpaste:
        log.error("no --file given and pbpaste is unavailable; pipe the capture on stdin")
        return "", "the clipboard"
    import subprocess

    try:
        out = subprocess.run([pbpaste], capture_output=True, text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        log.error("could not read the clipboard: %s", exc)
        return "", "the clipboard"
    return out, "the clipboard"


def _preview(raw: str, limit: int = 120) -> str:
    """Show what was actually read, so the cause is obvious without a second command."""
    flat = " ".join(raw.split())
    return (flat[:limit] + "...") if len(flat) > limit else (flat or "(nothing)")


def _cmd_jobs(store: JobStore, limit: int) -> int:
    jobs = store.list_jobs(limit=limit)
    if not jobs:
        print("no jobs yet")
        return 0
    for job in jobs:
        suffix = f"  ({job.error[:60]})" if job.error else ""
        print(f"{job.id}  {job.state:18s} {job.asin or '-':12s} {suffix}")
    return 0


def _cmd_bot(cfg: Config, store: JobStore) -> int:
    if cfg.telegram is None:
        log.error("no Telegram token. Set TELEGRAM_BOT_TOKEN or telegram.bot_token in config.toml")
        return 2
    from .bot.telegram import run_bot

    run_bot(cfg, store)
    return 0


def _cmd_doctor(cfg: Config) -> int:
    from .render.cards import FontMissing, resolve_fonts

    ok = True
    print(f"job database   {cfg.paths.db}")
    print(f"work dir       {cfg.paths.work}")
    print(f"output dir     {cfg.paths.output}")
    print(f"asset source   {cfg.source}")
    print(f"script backend {cfg.script.backend}"
          + ("  (your Claude plan)" if cfg.script.backend == "agent" else "  (API credits)"))
    print(f"dry run        {cfg.dry_run}")

    for name, binary in (("ffmpeg", cfg.ffmpeg), ("ffprobe", cfg.ffprobe)):
        path = shutil.which(binary)
        print(f"{name:14s} {path or 'MISSING - brew install ffmpeg'}")
        ok = ok and bool(path)

    try:
        print(f"encoder        {resolve_encoder(cfg)}")
    except ConfigError as exc:
        print(f"encoder        FAILED - {exc}")
        ok = False

    from .render.ffmpeg import has_filter

    if has_filter(cfg, "subtitles"):
        print("subtitles      available (libass)")
    else:
        print("subtitles      MISSING - ffmpeg has no libass; captions cannot be burned")
        print("               fix: brew reinstall ffmpeg")
        ok = False

    try:
        fonts = resolve_fonts(cfg.paths.assets)
        print(f"font           {fonts.bold}")
    except FontMissing as exc:
        print(f"font           MISSING - {exc}")
        ok = False

    # Count what pick_music would actually use, not every file in the folder:
    # the shipped .gitkeep is a file, and reporting it as a track promises music
    # that never plays.
    from .pipeline import MUSIC_SUFFIXES

    music_dir = cfg.paths.assets / "music"
    tracks = (
        [m for m in music_dir.iterdir() if m.is_file() and m.suffix.lower() in MUSIC_SUFFIXES]
        if music_dir.is_dir()
        else []
    )
    print(f"music tracks   {len(tracks)}{'  (none - videos will have no music bed)' if not tracks else ''}")
    print(f"affiliate tag  {cfg.affiliate_tag or 'NOT SET'}")

    import os

    needs_keys = not cfg.dry_run
    has_voice = bool(cfg.voice.api_key) and bool(cfg.voice.voice_id)
    has_anthropic, anthropic_via = _anthropic_credential(cfg)

    print(f"voice id       {cfg.voice.voice_id or 'NOT SET - copy it from the ElevenLabs voice library'}")
    print(f"ELEVENLABS_API_KEY {_describe_key(cfg.voice.api_key, cfg.voice.api_key_source)}")
    if cfg.voice.api_key and cfg.voice.api_key_source == "environment":
        print("               the environment overrides [voice] api_key in config.toml")
    print(f"ANTHROPIC_API_KEY  {anthropic_via}")
    print(f"telegram       {'configured' if cfg.telegram else 'not configured'}")
    print(f"youtube        {_youtube_status(cfg)}")

    if not needs_keys:
        print("\n  dry_run is on: no paid API calls, silent audio, full render.")
        return 0 if ok else 1

    if not has_voice:
        print("\n  Voice is not configured, so a real run fails at the voiceover step.")
        print("  Set ELEVENLABS_API_KEY and voice.voice_id, or set dry_run = true.")
        ok = False
    if cfg.script.backend == "agent" and os.environ.get("ANTHROPIC_API_KEY"):
        print("\n  ANTHROPIC_API_KEY is set while backend = \"agent\".")
        print("  Remove it, and [script] api_key, or this bills API credits, not your plan.")
        ok = False
    if not has_anthropic:
        print("\n  No Anthropic credential, so a real run fails at the script step.")
        print("  Set [script] api_key in config.toml, export ANTHROPIC_API_KEY,")
        print("  run `ant auth login`, or set dry_run = true.")
        ok = False

    return 0 if ok else 1


def _youtube_status(cfg: Config) -> str:
    if not cfg.youtube.get("enabled"):
        return "off (videos stay on this Mac)"
    token = Path(cfg.youtube.get("token_path", "~/.config/amzvid/youtube-token.json")).expanduser()
    privacy = cfg.youtube.get("privacy_status", "private")
    if not token.exists():
        return f"on, but NOT AUTHORIZED - run: amzvid youtube-auth  (uploads as {privacy})"
    return f"on, authorized, uploads as {privacy}"


def _describe_key(key: str, source: str) -> str:
    """Fingerprint rather than hide.

    "set" is true of a leftover placeholder as much as a real key, and a 401 from a
    freshly issued key is impossible to explain without seeing which value is in play.
    The first few characters and the length identify it without printing a secret.
    """
    if not key:
        return "not set"
    looks_real = key.startswith("sk_") and len(key) > 30
    shape = f"{key[:3]}... {len(key)} chars"
    note = "" if looks_real else "  <- does not look like an ElevenLabs key"
    return f"set ({source}, {shape}){note}"


def _anthropic_credential(cfg: Config) -> tuple[bool, str]:
    """From the config file, the environment, or an `ant auth login` profile."""
    import os

    if cfg.script.backend == "agent":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return True, "set - WARNING: this routes the agent backend to API credits"
        return True, "not needed (agent backend uses your signed-in Claude plan)"
    if cfg.script.api_key:
        return True, "set (config.toml, [script] api_key)"
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            return True, f"set (${var})"
    profile = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "anthropic"
    if profile.is_dir() and any(profile.iterdir()):
        return True, "set (ant auth login profile)"
    return False, "NOT SET - export ANTHROPIC_API_KEY or run: ant auth login"


def _cmd_youtube_auth(cfg: Config) -> int:
    """Run the consent flow once and store the token. Needs a browser on this machine."""
    from .publish.youtube import YouTubeError, authorize

    try:
        authorize(cfg, interactive=True)
    except (YouTubeError, ConfigError) as exc:
        log.error("%s", exc)
        return 1
    print("YouTube authorized. Uploads will go to the channel you just signed in as.")
    return 0


def _doctor_without_config(exc: ConfigError) -> int:
    """Degraded report when the config itself cannot be loaded."""
    print(f"config         BROKEN - {exc}")
    for name, binary in (("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe")):
        path = shutil.which(binary)
        print(f"{name:14s} {path or 'MISSING - brew install ffmpeg'}")
    print("\n  Fix the config above, then run `amzvid doctor` again.")
    return 1


def _stage_images(source: Path, inbox: Path) -> int:
    paths = sorted(p for p in source.iterdir() if p.is_file()) if source.is_dir() else [source]
    count = 0
    for i, path in enumerate(paths):
        shutil.copy2(path, inbox / f"{i:02d}_{path.name}")
        count += 1
    return count


if __name__ == "__main__":
    raise SystemExit(main())
