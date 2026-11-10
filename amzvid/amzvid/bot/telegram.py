"""Telegram front end. Long polling, so the Mac Mini needs no open port or public DNS.

One conversation per product: send the affiliate link, drop photos and notes, then /go.
The script comes back for approval before anything is voiced or rendered, which is the
only step where a human eye is actually worth the wait.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import Config
from ..db import Job, JobStore
from ..pipeline import load_draft, run_render_stage, run_script_stage
from ..plan import script_preview
from ..sources import extract_asin, resolve_short_link
from ..sources.clipboard import PayloadError, ingest, looks_like_payload, parse_payload

log = logging.getLogger(__name__)

TELEGRAM_UPLOAD_LIMIT = 48 * 1024 * 1024
CAPTURE_MERGE_WINDOW_S = 60 * 60  # a later capture of the same product adds to it
GOOD_PHOTO_COUNT = 5             # below this, suggest another capture
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
TELEGRAM_UPLOAD_TIMEOUT = 900.0  # seconds; a slow home uplink still finishes
URL_HINT = ("amazon.", "amzn.to", "a.co/")

HELP = """\
*How this works*

*Fast way:* capture the listing (bookmarklet on a Mac, Shortcut on a phone) and paste
what it copied into this chat. Title, bullets and price arrive at once.

On a phone the page only holds the photos near the viewport, so swipe the gallery and
capture again - captures of the same product within the hour add to the same job. Then
add notes and /go

*Manual way:*
1. Send me an Amazon affiliate link.
2. Send the product photos (as photos or files). Two or more.
3. Optionally send any notes as plain text - these matter more than the listing copy.
4. Send /go

I write the script, show it to you, and render only once you approve.

Commands
/go - write the script for the job you are building
/title <text> - set the product title
/price <text> - set the on-screen price
/status - what the current job is doing
/clearphotos - drop the photos, keep the text
/cancel - drop the job you are building
/jobs - recent jobs
"""


def run_bot(cfg: Config, store: JobStore) -> None:
    if cfg.telegram is None:
        raise RuntimeError("telegram is not configured")

    # The library defaults to a 5 second write timeout, which no finished video will
    # ever upload within: a 70 second 1080p cut is 10-25MB. Long polling needs its own
    # client so a slow upload cannot stall the update loop.
    app = (
        Application.builder()
        .token(cfg.telegram.bot_token)
        .request(HTTPXRequest(
            connect_timeout=30.0,
            read_timeout=TELEGRAM_UPLOAD_TIMEOUT,
            write_timeout=TELEGRAM_UPLOAD_TIMEOUT,
            # media_write_timeout is the one that governs a file upload; write_timeout
            # alone leaves the default of a few seconds in place for media.
            media_write_timeout=TELEGRAM_UPLOAD_TIMEOUT,
            pool_timeout=60.0,
        ))
        .get_updates_request(HTTPXRequest(connect_timeout=30.0, read_timeout=60.0))
        .build()
    )
    app.bot_data["cfg"] = cfg
    app.bot_data["store"] = store
    app.bot_data["render_lock"] = asyncio.Lock()

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("title", cmd_title))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("clearphotos", cmd_clearphotos))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("bot up; polling")
    app.run_polling(drop_pending_updates=True)


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #

def _authorized(update: Update, cfg: Config) -> bool:
    allowed = cfg.telegram.allowed_user_ids if cfg.telegram else ()
    if not allowed:
        return True
    user = update.effective_user
    return bool(user and user.id in allowed)


async def _deny(update: Update) -> None:
    user = update.effective_user
    log.warning("rejected update from user %s", user.id if user else "unknown")
    if update.effective_message:
        await update.effective_message.reply_text("Not authorized.")


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> tuple[Config, JobStore]:
    return context.bot_data["cfg"], context.bot_data["store"]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, _store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)


async def cmd_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_meta_field(update, context, "title", " ".join(context.args or []))


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_meta_field(update, context, "price", " ".join(context.args or []))


async def _set_meta_field(
    update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, value: str
) -> None:
    cfg, store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)
    if not value.strip():
        return await update.effective_message.reply_text(f"Usage: /{field} <text>")

    job = store.latest_collecting(update.effective_chat.id)
    if job is None:
        return await update.effective_message.reply_text("Send an Amazon link first.")
    _patch_product_meta(cfg, job, {field: value.strip()})
    await update.effective_message.reply_text(f"{field} set.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)
    job = store.latest_for_chat(update.effective_chat.id)
    if job is None:
        return await update.effective_message.reply_text("No jobs yet. Send me an Amazon link.")
    extra = f"\nerror: {job.error}" if job.error else ""
    inbox = cfg.paths.job_dir(job.id) / "inbox"
    count = len(list(inbox.glob("*"))) if inbox.exists() else 0
    await update.effective_message.reply_text(
        f"{job.id}\nstate: {job.state}\nasin: {job.asin or 'unknown'}\nphotos: {count}{extra}"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)
    job = store.latest_for_chat(update.effective_chat.id, ("collecting", "awaiting_approval"))
    if job is None:
        return await update.effective_message.reply_text("Nothing to cancel.")
    store.set_state(job.id, "cancelled", "cancelled from telegram")
    await update.effective_message.reply_text(f"Cancelled {job.id}.")


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)
    jobs = store.list_jobs(limit=10)
    if not jobs:
        return await update.effective_message.reply_text("No jobs yet.")
    lines = [f"{j.id}  {j.state}  {j.asin or '-'}" for j in jobs]
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_clearphotos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop the photos on the current job, keeping its title, bullets and price.

    A capture can return the wrong images while getting the text right; this makes the
    text worth keeping without the pictures coming along.
    """
    cfg, store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)

    job = store.latest_collecting(update.effective_chat.id)
    if job is None:
        return await update.effective_message.reply_text("No job in progress.")

    job_dir = cfg.paths.job_dir(job.id)
    inbox = job_dir / "inbox"
    removed = 0
    if inbox.exists():
        for path in sorted(inbox.iterdir()):
            if path.is_file() and path.suffix.lower() in PHOTO_SUFFIXES:
                path.unlink()
                removed += 1
    meta_path = job_dir / "product.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["image_ids"] = []
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    await update.effective_message.reply_text(
        f"Dropped {removed} photo(s). The title, bullets and price are kept. "
        "Send the photos you want, then /go"
    )


async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)

    message = update.effective_message
    job = store.latest_collecting(update.effective_chat.id)
    if job is None:
        return await message.reply_text("Send an Amazon link first.")

    inbox = cfg.paths.job_dir(job.id) / "inbox"
    photos = len(list(inbox.glob("*"))) if inbox.exists() else 0
    if cfg.source == "manual" and photos < 2:
        return await message.reply_text(f"Only {photos} photo(s) so far. Send at least two.")

    await message.reply_text(f"Writing the script for {job.asin or job.id}...")
    await _write_script(update, context, job)


async def _write_script(update: Update, context: ContextTypes.DEFAULT_TYPE, job: Job) -> None:
    cfg, store = _ctx(context)
    message = update.effective_message
    store.set_state(job.id, "scripting")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        _product, draft = await asyncio.to_thread(run_script_stage, cfg, job.id, job.affiliate_url)
    except Exception as exc:  # surfaced to the operator, logged with the traceback
        log.exception("script stage failed for %s", job.id)
        store.fail(job.id, str(exc))
        return await message.reply_text(f"Script failed:\n{exc}")

    store.set_state(job.id, "awaiting_approval")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Render", callback_data=f"render:{job.id}"),
                InlineKeyboardButton("Rewrite", callback_data=f"redo:{job.id}"),
            ],
            [InlineKeyboardButton("Cancel", callback_data=f"cancel:{job.id}")],
        ]
    )
    for part in _split(script_preview(draft)):
        await message.reply_text(part)
    await message.reply_text(
        f"{len(draft.scenes)} scenes, {sum(len(s.narration.split()) for s in draft.scenes)} words. "
        f"Render 1 long + {len(draft.shorts)} shorts?",
        reply_markup=keyboard,
    )


# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)

    message = update.effective_message
    text = (message.text or "").strip()
    chat_id = update.effective_chat.id

    if looks_like_payload(text):
        return await _on_capture(update, context, text)

    if any(hint in text.lower() for hint in URL_HINT):
        url = next((tok for tok in text.split() if any(h in tok.lower() for h in URL_HINT)), text)
        asin = extract_asin(await asyncio.to_thread(resolve_short_link, url))
        job = store.create(url, asin, chat_id)
        job_dir = cfg.paths.job_dir(job.id)
        (job_dir / "inbox").mkdir(parents=True, exist_ok=True)
        _patch_product_meta(cfg, job, {})
        return await message.reply_text(
            f"Job {job.id} for {asin or 'unknown ASIN'}.\n"
            "Now send the photos, then any notes, then /go"
        )

    job = store.latest_collecting(chat_id)
    if job is None:
        return await message.reply_text("Send an Amazon link to start a job, or /help.")

    meta = _read_product_meta(cfg, job)
    notes = (meta.get("notes", "") + "\n" + text).strip()
    _patch_product_meta(cfg, job, {"notes": notes})
    await message.reply_text("Noted.")


async def _on_capture(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """A blob from the capture bookmarklet: everything arrives at once, no photos needed."""
    cfg, store = _ctx(context)
    message = update.effective_message
    try:
        payload = parse_payload(text)
    except PayloadError as exc:
        return await message.reply_text(f"That capture did not parse:\n{exc}")

    # A phone only ever holds the two or three photos near the viewport, so a second
    # capture of the same product adds to the first rather than starting over.
    existing = store.recent_for_asin(
        update.effective_chat.id, payload["asin"], ("collecting", "queued"), CAPTURE_MERGE_WINDOW_S
    )
    job = existing or store.create(payload["url"], payload["asin"], update.effective_chat.id)

    await message.reply_text(
        ("Adding to " if existing else "Fetching ")
        + f"{len(payload['images'])} photo(s) for {payload['asin']}..."
    )
    try:
        count = await asyncio.to_thread(
            ingest, cfg, payload, cfg.paths.job_dir(job.id), "", merge=bool(existing)
        )
    except Exception as exc:
        log.exception("capture ingest failed for %s", job.id)
        store.fail(job.id, str(exc))
        return await message.reply_text(f"Could not fetch the photos:\n{exc}")

    if not count:
        store.fail(job.id, "no images could be downloaded")
        return await message.reply_text("None of the image URLs downloaded. Try the capture again.")

    tail = (
        "\n\nSwipe the gallery and capture again to add more photos, or /go to write "
        "the script."
        if count < GOOD_PHOTO_COUNT
        else "\n\nSend any notes as plain text, then /go"
    )
    await message.reply_text(
        f"{job.id}\n{payload['title'][:120]}\n"
        f"{count} photo(s) held, {len(payload['bullets'])} bullet(s)"
        + (f", {payload['price']}" if payload["price"] else "")
        + tail
    )


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store = _ctx(context)
    if not _authorized(update, cfg):
        return await _deny(update)

    message = update.effective_message
    job = store.latest_collecting(update.effective_chat.id)
    if job is None:
        return await message.reply_text("Send an Amazon link first, then the photos.")

    inbox = cfg.paths.job_dir(job.id) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    if message.photo:
        tg_file = await message.photo[-1].get_file()  # largest rendition
        suffix = ".jpg"
    else:
        tg_file = await message.document.get_file()
        suffix = Path(message.document.file_name or "image.jpg").suffix or ".jpg"

    # Continue the capture's img_NN sequence rather than starting a parallel one:
    # the manual source reads the inbox in name order, and two naming schemes meant
    # sent photos sorted ahead of captured ones and silently became the hero shot.
    existing = _photo_count(inbox)
    dest = inbox / f"img_{existing:02d}{suffix.lower()}"
    await tg_file.download_to_drive(custom_path=dest)
    log.info("job %s: saved %s", job.id, dest.name)

    total = existing + 1
    if total in (2, 5, 8):
        await message.reply_text(f"{total} photos. Send more, or /go")


def _photo_count(inbox: Path) -> int:
    """Image files only - the capture leaves a _raw directory in there too."""
    return len([p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES])


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg, store = _ctx(context)
    query = update.callback_query
    await query.answer()
    if not _authorized(update, cfg):
        return await _deny(update)

    action, _, job_id = (query.data or "").partition(":")
    try:
        job = store.get(job_id)
    except KeyError:
        return await query.edit_message_text("That job is gone.")

    if action == "cancel":
        store.set_state(job_id, "cancelled", "cancelled from telegram")
        return await query.edit_message_text(f"Cancelled {job_id}.")

    if action == "redo":
        await query.edit_message_text("Rewriting...")
        return await _write_script(update, context, job)

    if action != "render":
        return await query.edit_message_text(f"Unknown action: {action}")

    lock: asyncio.Lock = context.bot_data["render_lock"]
    if lock.locked():
        await query.edit_message_text("Another render is running. This one is queued.")
    else:
        await query.edit_message_text("Rendering. This takes a few minutes.")

    async with lock:
        await _render(update, context, job)


async def _render(update: Update, context: ContextTypes.DEFAULT_TYPE, job: Job) -> None:
    cfg, store = _ctx(context)
    chat_id = update.effective_chat.id
    store.set_state(job.id, "rendering")

    try:
        from ..sources import get_source

        product = await asyncio.to_thread(get_source(cfg).fetch, job.affiliate_url, cfg.paths.job_dir(job.id))
        draft = load_draft(cfg, job.id)
        results = await asyncio.to_thread(run_render_stage, cfg, job.id, product, draft)
    except Exception as exc:
        log.exception("render failed for %s", job.id)
        store.fail(job.id, str(exc))
        return await context.bot.send_message(chat_id, f"Render failed:\n{exc}")

    store.set_state(job.id, "done")
    out_dir = cfg.paths.out_dir(job.id)

    # When the videos are already on YouTube there is nothing to send: moving a 44MB
    # file to the phone so it can be uploaded again is the problem this replaces.
    uploaded = [item for item in results if item.youtube_url]
    if uploaded:
        lines = [f"{item.kind}: {item.youtube_url}" for item in uploaded]
        await context.bot.send_message(
            chat_id,
            "Uploaded to YouTube as drafts. Open YouTube Studio to review and publish.\n\n"
            + "\n".join(lines),
            disable_web_page_preview=False,
        )

    for item in results:
        if item.youtube_url:
            continue
        size = item.path.stat().st_size
        caption = f"{item.kind} · {item.duration_s:.0f}s · {item.title}"[:1000]
        if size > TELEGRAM_UPLOAD_LIMIT:
            await context.bot.send_message(chat_id, f"{item.path.name} is {size / 1e6:.0f}MB - too big to send.\n{item.path}")
            continue
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            with item.path.open("rb") as handle:
                await context.bot.send_video(
                    chat_id,
                    video=handle,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=TELEGRAM_UPLOAD_TIMEOUT,
                    write_timeout=TELEGRAM_UPLOAD_TIMEOUT,
                    connect_timeout=30.0,
                    pool_timeout=60.0,
                )
        except TelegramError as exc:
            # The render succeeded; only the upload did not. Say so, because the file is
            # sitting there and the message otherwise reads like the job failed.
            await context.bot.send_message(
                chat_id,
                f"Rendered fine, but could not upload {item.path.name} ({size / 1e6:.0f}MB): "
                f"{exc}\nIt is on the Mac at:\n{item.path}",
            )

    description = out_dir / "long.description.txt"
    if description.exists():
        for part in _split(description.read_text()):
            await context.bot.send_message(chat_id, part)

    thumb = out_dir / "thumbnail.jpg"
    if thumb.exists():
        with thumb.open("rb") as handle:
            await context.bot.send_photo(chat_id, photo=handle, caption="thumbnail")

    await context.bot.send_message(chat_id, f"Done. Files are in:\n{out_dir}")


# --------------------------------------------------------------------------- #

def _read_product_meta(cfg: Config, job: Job) -> dict:
    path = cfg.paths.job_dir(job.id) / "product.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _patch_product_meta(cfg: Config, job: Job, patch: dict) -> dict:
    job_dir = cfg.paths.job_dir(job.id)
    job_dir.mkdir(parents=True, exist_ok=True)
    meta = _read_product_meta(cfg, job)
    meta.setdefault("title", "")
    meta.setdefault("price", "")
    meta.setdefault("notes", "")
    meta.setdefault("bullets", [])
    meta.update(patch)
    (job_dir / "product.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def _split(text: str, limit: int = 3800) -> list[str]:
    """Telegram caps a message at 4096 characters; split on blank lines."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    length = 0
    for block in text.split("\n"):
        if length + len(block) + 1 > limit and current:
            parts.append("\n".join(current))
            current, length = [], 0
        current.append(block)
        length += len(block) + 1
    if current:
        parts.append("\n".join(current))
    return parts
