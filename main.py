import asyncio
import os
import re
import tempfile
import time
import hashlib
from telegram import Update, InputFile
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from yt_dlp import YoutubeDL

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TELEGRAM_MAX_FILE_MB = 45
TELEGRAM_MAX_FILE_BYTES = TELEGRAM_MAX_FILE_MB * 1024 * 1024
FORMAT_CHAIN = [
    'bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/b[height<=480]/b',
    'b[height<=360]/bv*[height<=360]+ba/b',
    'bestaudio[ext=m4a]/bestaudio'
]
DEDUP_TTL = 3 * 60 * 60  # 3 hours
# ===========================================

chat_locks = {}
dedup_seen = {}

# ------------ Helpers ------------
def _yt_url(text: str):
    pattern = r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=[\w-]+|youtu\.be/[\w-]+)[^\s]*)'
    m = re.search(pattern, text)
    return m.group(1) if m else None

def _hash_key(s: str) -> str:
    return hashlib.sha256(s.strip().encode()).hexdigest()[:16]

def _dedup_check(chat_id: int, url: str) -> bool:
    now = time.time()
    store = dedup_seen.setdefault(chat_id, {})
    for k in list(store.keys()):
        if now - store[k] > DEDUP_TTL:
            del store[k]
    key = _hash_key(url)
    if key in store:
        return True
    store[key] = now
    return False

async def _send_action(update: Update, context, action):
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=action)
    except:
        pass

# ------------ Core Handlers ------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Namaste! Mujhe YouTube link bhejo ya /get <url> likho.\n"
        "480p target, fallback 360p, ya sirf audio agar size zyada hua."
    )

async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else update.message.text
    url = _yt_url(text or "")
    if not url:
        await update.message.reply_text("❌ Valid YouTube URL bhejiye.")
        return
    await process_url(update, context, url)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = _yt_url(update.message.text or "")
    if url:
        await process_url(update, context, url)

async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    cid = update.effective_chat.id
    lock = chat_locks.setdefault(cid, asyncio.Lock())

    if _dedup_check(cid, url):
        await update.message.reply_text("⚠️ Ye link abhi thodi der pehle process hua tha, skip kar raha hoon.")
        return

    async with lock:
        await _send_action(update, context, ChatAction.TYPING)
        await update.message.reply_text("🔍 Video info fetch kar raha hoon...")

        info = await extract_info(url)
        if not info:
            await update.message.reply_text("❌ Video info fetch nahi ho paya.")
            return

        title = info.get("title") or "video"
        duration = info.get("duration")

        for fmt in FORMAT_CHAIN:
            try:
                await update.message.reply_text(f"🎯 Trying format: {fmt}")
                path = await download_format(url, fmt)
                size = os.path.getsize(path)

                if size > TELEGRAM_MAX_FILE_BYTES:
                    await update.message.reply_text(
                        f"📦 {size // (1024*1024)}MB > {TELEGRAM_MAX_FILE_MB}MB limit. Lower quality try kar raha hoon..."
                    )
                    os.remove(path)
                    continue

                caption = f"{title}"
                if duration:
                    caption += f" | ⏱ {duration//60}m {duration%60}s"

                await _send_action(update, context, ChatAction.UPLOAD_VIDEO)
                if path.endswith((".m4a", ".mp3", ".opus")):
                    await context.bot.send_audio(chat_id=cid, audio=InputFile(path), caption=caption[:1024])
                else:
                    await context.bot.send_video(chat_id=cid, video=InputFile(path), caption=caption[:1024], supports_streaming=True)

                os.remove(path)
                await update.message.reply_text("✅ Done!")
                return

            except Exception as e:
                continue

        await update.message.reply_text("❌ Saari formats limit cross kar rahi hain.")

# ------------ yt-dlp Async Wrappers ------------
async def extract_info(url: str):
    opts = {"quiet": True, "nocheckcertificate": True, "skip_download": True, "noplaylist": True}
    loop = asyncio.get_running_loop()
    with YoutubeDL(opts) as ydl:
        return await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))

async def download_format(url: str, fmt: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="ytb_")
    outtmpl = os.path.join(tmpdir, "%(title).80s.%(ext)s")
    opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "nocheckcertificate": True
    }
    loop = asyncio.get_running_loop()
    def _do_download():
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            real_path = ydl.prepare_filename(info)
            for ext in (".mp4", ".m4a", ".mp3", ".opus", ".webm"):
                candidate = os.path.splitext(real_path)[0] + ext
                if os.path.exists(candidate):
                    return candidate
            raise FileNotFoundError("Downloaded file not found")
    return await loop.run_in_executor(None, _do_download)

# ------------ Main Entrypoint ------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var!")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("get", get_cmd))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
