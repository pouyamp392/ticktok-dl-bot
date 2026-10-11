
import os
import re
import logging
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_FILE_SIZE = 45 * 1024 * 1024  # 45 MB


def is_tiktok_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in ("http", "https")
            and (
                host == "tiktok.com"
                or host.endswith(".tiktok.com")
            )
        )
    except ValueError:
        return False


def download_video(url: str, folder: str) -> Path:
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = Path(ydl.prepare_filename(info))

        if not filename.exists():
            candidates = list(Path(folder).glob("*"))
            candidates = [
                item for item in candidates if item.is_file()
            ]
            if not candidates:
                raise FileNotFoundError("Downloaded file not found")
            filename = max(candidates, key=lambda item: item.stat().st_size)

        if filename.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("FILE_TOO_LARGE")

        return filename


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "لینک یک ویدئوی TikTok که اجازهٔ دانلودش را داری بفرست.\n"
        "بات تلاش می‌کند فایل قابل‌دریافت را برایت ارسال کند."
    )


async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message or not message.text:
        return

    match = re.search(r"https?://\S+", message.text)
    if not match:
        await message.reply_text("لطفاً یک لینک معتبر TikTok بفرست.")
        return

    url = match.group(0).rstrip(".,!?)]}")
    if not is_tiktok_url(url):
        await message.reply_text("این لینک متعلق به دامنهٔ TikTok نیست.")
        return

    status = await message.reply_text("⏳ در حال بررسی و دریافت ویدئو...")

    try:
        with tempfile.TemporaryDirectory(prefix="tiktok_") as folder:
            video_path = await asyncio.to_thread(
                download_video, url, folder
            )

            if video_path.stat().st_size > MAX_FILE_SIZE:
                raise ValueError("FILE_TOO_LARGE")

            await status.edit_text("📤 دریافت شد؛ در حال ارسال به تلگرام...")

            with video_path.open("rb") as video:
                await message.reply_document(
                    document=video,
                    filename=video_path.name,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    caption="ویدئوی دریافت‌شده از TikTok",
                )

            await status.delete()

    except ValueError as exc:
        if str(exc) == "FILE_TOO_LARGE":
            await status.edit_text(
                "حجم ویدئو برای این نسخه بیشتر از حد مجاز است."
            )
        else:
            logger.exception("Invalid download result")
            await status.edit_text("نتوانستم این ویدئو را دریافت کنم.")

    except Exception:
        logger.exception("TikTok download failed")
        await status.edit_text(
            "❌ دانلود ناموفق بود. ممکن است لینک خصوصی، حذف‌شده "
            "یا با محدودیت دسترسی روبه‌رو باشد. کمی بعد دوباره امتحان کن."
        )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled bot error",
        exc_info=context.error,
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN در متغیرهای محیطی تنظیم نشده است.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )
    app.add_error_handler(error_handler)

    logger.info("TikTok bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
