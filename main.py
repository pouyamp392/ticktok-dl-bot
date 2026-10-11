
import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
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

# جلوگیری از چاپ درخواست‌های HTTP و URLهای حاوی توکن در لاگ
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
        path
        for path in Path(folder).iterdir()
        if path.is_file()
        and not path.name.endswith((".part", ".ytdl"))
    ]

    if not files:
        raise RuntimeError("Download completed without producing a file")

    # اگر چند فایل وجود داشت، بزرگ‌ترین فایل را انتخاب کن.
    video_path = max(files, key=lambda path: path.stat().st_size)

    if video_path.stat().st_size > 45 * 1024 * 1024:
        raise ValueError("Video exceeds the 45 MB limit")

    return str(video_path)


# ----------------------------
# Telegram commands
# ----------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        "سلام! 👋\n\n"
        "لینک ویدئوی TikTok رو بفرست تا دانلودش رو امتحان کنم.\n\n"
        "توجه: همهٔ ویدئوها لزوماً قابل دریافت نیستند."
    )


async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.text:
        return

    url = extract_tiktok_url(update.message.text)

    if not url:
        await update.message.reply_text(
            "لطفاً یک لینک معتبر از TikTok بفرست."
        )
        return

    status_message = await update.message.reply_text(
        "⏳ در حال دریافت ویدئو..."
    )

    folder = tempfile.mkdtemp(prefix="tiktok_")

    try:
        video_path = await asyncio.to_thread(
            download_video,
            url,
            folder,
        )

        await status_message.edit_text(
            "✅ دانلود انجام شد؛ در حال ارسال ویدئو..."
        )

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
        # لینک و توکن را در لاگ چاپ نمی‌کنیم.
        logger.exception("TikTok download failed")

        try:
            await status_message.edit_text(
                "❌ دانلود ناموفق بود.\n\n"
                "ممکنه TikTok دسترسی به این ویدئو رو محدود کرده "
                "باشه یا yt-dlp نیاز به به‌روزرسانی داشته باشه. "
                "لطفاً بعداً دوباره امتحان کن."
            )
        except Exception:
            logger.warning("Could not update status message")

    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled Telegram error",
        exc_info=(
            type(context.error),
            context.error,
            context.error.__traceback__,
        ) if context.error else None,
    )


# ----------------------------
# Main application
# ----------------------------

def main():
    token = os.environ.get("BOT_TOKEN")

    if not token:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it in Render Environment Variables."
        )

    # اجرای وب‌سرور برای Render
    threading.Thread(
        target=run_web_server,
        daemon=True,
        name="render-web-server",
    ).start()

    # اجرای ربات تلگرام
    telegram_app = (
        Application.builder()
        .token(token)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler("start", start)
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link,
        )
    )

    telegram_app.add_error_handler(error_handler)

    logger.info("TikTok bot is starting...")

    telegram_app.run_polling()


if __name__ == "__main__":
    main()
