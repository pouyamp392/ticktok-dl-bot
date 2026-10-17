import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from flask import Flask
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------
# Logging
# ----------------------------

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ----------------------------
# Environment / central mandatory join API
# ----------------------------

ADMIN_API_URL = os.getenv(
    "ADMIN_API_URL", "https://admin-dl-bot.onrender.com"
).rstrip("/")
API_KEY = os.getenv("API_KEY", "")

# ⚠️ متن دکمه استارت — همه‌جا از همین استفاده می‌شود
START_BUTTON_TEXT = "🚀 استارت"

# ----------------------------
# Render Web Server
# ----------------------------

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "TikTok Bot is running!", 200


@web_app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_web_server():
    port = int(os.environ.get("PORT", "10000"))
    logger.info("Starting web server on port %s", port)
    web_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


# ----------------------------
# Bot Commands (منوی آبی کنار کیبورد)
# ----------------------------

async def setup_bot_commands(app: Application):
    commands = [
        BotCommand("start", START_BUTTON_TEXT),
    ]

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeDefault(),
    )

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    logger.info("Bot commands configured.")


# ----------------------------
# Reply Keyboard (کیبورد پایین)
# ----------------------------

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(START_BUTTON_TEXT)],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="لینک تیک‌تاک را بفرست یا استارت بزن...",
    )


# ----------------------------
# TikTok URL validation
# ----------------------------

URL_PATTERN = re.compile(r"https?://[^\s<>]+")


def extract_tiktok_url(text: str):
    match = URL_PATTERN.search(text or "")
    if not match:
        return None

    url = match.group(0).rstrip(".,!?;:)]}\"'")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https"):
            return None
        if host == "tiktok.com" or host.endswith(".tiktok.com"):
            return url
    except ValueError:
        return None
    return None


# ----------------------------
# Video downloader
# ----------------------------

def download_video(url: str, folder: str) -> str:
    output_template = str(Path(folder) / "%(id)s.%(ext)s")
    options = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "max_filesize": 45 * 1024 * 1024,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "quiet": True,
        "no_warnings": True,
        "logger": logger,
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.extract_info(url, download=True)

    files = [
        path for path in Path(folder).iterdir()
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    ]
    if not files:
        raise RuntimeError("Download completed without producing a file")

    video_path = max(files, key=lambda path: path.stat().st_size)
    if video_path.stat().st_size > 45 * 1024 * 1024:
        raise ValueError("Video exceeds the 45 MB limit")
    return str(video_path)


# ----------------------------
# Mandatory Join (Central Admin API)
# ----------------------------

async def check_membership(user_id: int):
    """Return (allowed, missing_channels). Fail closed if the API is unavailable."""
    if not API_KEY:
        logger.error("API_KEY is not configured; mandatory join check is blocked")
        return False, []

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/check",
                params={"user_id": user_id},
                headers={"X-API-Key": API_KEY},
            )
            response.raise_for_status()
            data = response.json()

        if data.get("enabled") is False:
            return True, []

        missing = data.get("missing") or []
        allowed = bool(data.get("ok")) and not missing
        return allowed, missing
    except Exception:
        logger.exception("Central membership API request failed")
        return False, []


def _channel_value(channel, *keys):
    if isinstance(channel, dict):
        for key in keys:
            value = channel.get(key)
            if value:
                return str(value)
        return ""
    return str(channel) if channel else ""


async def membership_markup(missing):
    """Build join buttons from missing channels and central API channel data."""
    rows = []
    channels = missing if all(isinstance(item, dict) for item in missing) else []

    if missing and not channels:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(
                    f"{ADMIN_API_URL}/api/channels",
                    headers={"X-API-Key": API_KEY},
                )
                response.raise_for_status()
                payload = response.json()

            all_channels = (
                payload.get("channels", payload)
                if isinstance(payload, dict)
                else payload
            )
            if isinstance(all_channels, list):
                wanted = {str(item).lstrip("@").lower() for item in missing}
                for channel in all_channels:
                    if not isinstance(channel, dict):
                        continue
                    identifiers = {
                        _channel_value(
                            channel, "id", "channel_id", "username", "channel", "chat_id"
                        ).lstrip("@").lower(),
                        _channel_value(
                            channel, "username", "channel", "chat_id"
                        ).lstrip("@").lower(),
                    }
                    if wanted.intersection(identifiers):
                        channels.append(channel)
        except Exception:
            logger.exception("Could not load channel buttons from central API")

    for channel in channels:
        url = _channel_value(
            channel, "join_url", "url", "invite_link", "channel_url", "link"
        )
        name = _channel_value(
            channel, "title", "name", "username", "channel"
        ) or "عضویت در کانال"
        if url.startswith(("http://", "https://")):
            rows.append([InlineKeyboardButton(f"عضویت در {name}", url=url)])

    rows.append([
        InlineKeyboardButton(
            "✅ عضو شدم؛ بررسی دوباره", callback_data="join:check"
        )
    ])
    return InlineKeyboardMarkup(rows)


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    allowed, missing = await check_membership(user.id)
    if allowed:
        return True

    await message.reply_text(
        "🔒 برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شو؛ "
        "سپس روی «عضو شدم؛ بررسی دوباره» بزن.",
        reply_markup=await membership_markup(missing),
    )
    return False


async def on_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    allowed, missing = await check_membership(query.from_user.id)
    if allowed:
        await query.edit_message_text(
            "✅ عضویتت تأیید شد. حالا لینک ویدئوی TikTok را برای ربات بفرست."
        )
    else:
        await query.edit_message_text(
            "❌ هنوز عضویتت تأیید نشده. در کانال‌های لازم عضو شو و دوباره بررسی کن.",
            reply_markup=await membership_markup(missing),
        )


# ----------------------------
# Telegram commands
# ----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await require_membership(update, context):
        return

    # ⚠️ کیبورد پایین همیشه همراه پیام استارت ارسال می‌شود
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "لینک ویدئوی TikTok رو بفرست تا دانلودش رو امتحان کنم.\n\n"
        "توجه: همهٔ ویدئوها لزوماً قابل دریافت نیستند.",
        reply_markup=get_main_reply_keyboard(),
    )


# ----------------------------
# هندلر دکمه استارت از کیبورد پایین
# ----------------------------

async def handle_reply_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    if message.text.strip() == START_BUTTON_TEXT:
        await start(update, context)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not await require_membership(update, context):
        return

    url = extract_tiktok_url(update.message.text)
    if not url:
        await update.message.reply_text("لطفاً یک لینک معتبر از TikTok بفرست.")
        return

    status_message = await update.message.reply_text("⏳ در حال دریافت ویدئو...")
    folder = tempfile.mkdtemp(prefix="tiktok_")

    try:
        video_path = await asyncio.to_thread(download_video, url, folder)
        await status_message.edit_text("✅ دانلود انجام شد؛ در حال ارسال ویدئو...")

        with open(video_path, "rb") as video_file:
            await update.message.reply_document(
                document=video_file,
                filename=Path(video_path).name,
                caption="دانلود با موفقیت انجام شد ✅",
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
        await status_message.delete()

    except Exception:
        logger.exception("TikTok download failed")
        try:
            await status_message.edit_text(
                "❌ دانلود ناموفق بود.\n\n"
                "ممکنه TikTok دسترسی به این ویدئو رو محدود کرده باشه یا "
                "yt-dlp نیاز به به‌روزرسانی داشته باشه. لطفاً بعداً دوباره امتحان کن."
            )
        except Exception:
            logger.warning("Could not update status message")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Unhandled Telegram error",
        exc_info=(
            type(context.error), context.error, context.error.__traceback__
        ) if context.error else None,
    )


# ----------------------------
# Main application
# ----------------------------

async def post_init(app: Application):
    """منوی آبی را از همان ابتدا تنظیم می‌کند."""
    await setup_bot_commands(app)


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it in Render Environment Variables."
        )

    threading.Thread(
        target=run_web_server,
        daemon=True,
        name="render-web-server",
    ).start()

    telegram_app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(
        CallbackQueryHandler(on_join_check, pattern=r"^join:check$")
    )

    # ⚠️ هندلر دکمه استارت از کیبورد پایین — دقیقاً قبل از handle_link
    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(
                rf"^{re.escape(START_BUTTON_TEXT)}$"
            ),
            handle_reply_keyboard,
        )
    )

    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )
    telegram_app.add_error_handler(error_handler)

    logger.info("TikTok bot is starting...")
    telegram_app.run_polling()


if __name__ == "__main__":
    main()
