import asyncio
import html
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events, types

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

VIDEO_POST_URL = "https://t.me/HARSHUREQASSETS/4"
APK_POST_URL = "https://t.me/HARSHUREQASSETS/3"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requests.sqlite3")

WELCOME_TEXT = (
    '<tg-emoji emoji-id="5363992034728229166">✨</tg-emoji> 𝗛𝗘𝗟𝗟𝗢 {name} '
    '<tg-emoji emoji-id="6312103973671147302">👑</tg-emoji>'
    '<tg-emoji emoji-id="5472055112702629499">👋</tg-emoji>\n'
    '𝗔𝗔𝗣𝗞𝗜 𝗥𝗘𝗤𝗨𝗘𝗦𝗧 𝗝𝗔𝗟𝗗𝗜 𝗛𝗜 𝗔𝗣𝗣𝗥𝗢𝗩𝗘 𝗛𝗢 𝗝𝗔𝗬𝗘𝗚𝗜 '
    '<tg-emoji emoji-id="6327678689022579783">😄</tg-emoji>\n'
    '\n'
    '𝗦𝗘𝗧𝗨𝗣 𝗩𝗜𝗗𝗘𝗢 & 𝗛𝗔𝗖𝗞 𝗔𝗣𝗞 𝗡𝗘𝗘𝗖𝗛𝗘 𝗗𝗜𝗬𝗔 𝗚𝗔𝗬𝗔 𝗛𝗔𝗜 '
    '<tg-emoji emoji-id="6147439566107186310">👇</tg-emoji>'
)

# Sent by the bot only after the user voluntarily starts the bot.
INTRO_MESSAGE = "KITNE RUPEES KA LOSS HUA TUMHARA RECOVER KRNA CHAHTE HO?"

APK_BUTTONS = {
    "inline_keyboard": [
        [{
            "text": "DM FOR LOSS RECOVERY",
            "url": "https://t.me/m/MfujVZgiZDc1",
            "icon_custom_emoji_id": "5465476962266934316",
            "style": "danger",
        }],
        [{
            "text": "REGISTRATION LINK",
            "url": "https://www.dmplay20.com/#/register?invitationCode=25855773558",
            "icon_custom_emoji_id": "5231200819986047254",
            "style": "success",
        }],
        [{
            "text": "VIP GROUP",
            "url": "https://t.me/+1gF7gwo62VU4NTU1",
            "icon_custom_emoji_id": "5949775417274536507",
            "style": "primary",
        }],
    ]
}

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    force=True,
)
logging.getLogger("telethon").setLevel(logging.INFO)
log = logging.getLogger("joinbot")

# A persistent Telethon SQLite session keeps update state across restarts.
# The separate requests.sqlite3 file stores durable join-request/delivery state.
bot = TelegramClient("harshureqbot_bot", API_ID, API_HASH, auto_reconnect=True)

# Limit concurrent opt-in deliveries. This avoids flooding the Bot API during bursts.
delivery_semaphore = asyncio.Semaphore(2)
in_progress: set[int] = set()


class BotAPIError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None,
                 retry_after: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after
        self.retryable = retryable


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS request_queue (
                user_id INTEGER PRIMARY KEY,
                peer TEXT,
                source TEXT NOT NULL DEFAULT 'join_request',
                status TEXT NOT NULL DEFAULT 'awaiting_start',
                stage INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                last_error TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_queue_status "
            "ON request_queue(status, updated_at)"
        )
        conn.commit()


def save_join_request(user_id: int, peer: str) -> None:
    now = now_utc()
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO request_queue
                (user_id, peer, source, status, stage, request_count,
                 first_seen_at, last_seen_at, updated_at)
            VALUES (?, ?, 'join_request', 'awaiting_start', 0, 1, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                peer = excluded.peer,
                request_count = request_queue.request_count + 1,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (user_id, peer, now, now, now),
        )
        conn.commit()


def ensure_opt_in_record(user_id: int) -> None:
    now = now_utc()
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO request_queue
                (user_id, source, status, stage, request_count,
                 first_seen_at, last_seen_at, started_at, updated_at)
            VALUES (?, 'direct_start', 'delivery_pending', 0, 0, ?, ?, ?, ?)
            """,
            (user_id, now, now, now, now),
        )
        conn.execute(
            """
            UPDATE request_queue
            SET started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE user_id = ?
            """,
            (now, now, user_id),
        )
        conn.commit()


def get_request(user_id: int):
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM request_queue WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def save_stage(user_id: int, stage: int) -> None:
    status = "delivered" if stage >= 4 else "delivery_pending"
    with closing(db_connect()) as conn:
        conn.execute(
            """
            UPDATE request_queue
            SET stage = ?, status = ?, last_error = NULL, updated_at = ?
            WHERE user_id = ?
            """,
            (stage, status, now_utc(), user_id),
        )
        conn.commit()


def save_error(user_id: int, error: str) -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            """
            UPDATE request_queue
            SET status = 'retry', last_error = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (error[:1000], now_utc(), user_id),
        )
        conn.commit()


def parse_telegram_post_url(post_url: str):
    parsed = urlparse(post_url)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.netloc not in {"t.me", "www.t.me"}
        or len(parts) != 2
        or not parts[1].isdigit()
    ):
        raise ValueError(f"Unsupported Telegram post URL: {post_url}")
    return f"@{parts[0]}", int(parts[1])


def _parse_bot_response(raw_body: str, http_status: int | None = None):
    try:
        result = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise BotAPIError(
            f"Telegram returned invalid JSON (HTTP {http_status}): {raw_body[:300]}",
            error_code=http_status,
            retryable=bool(http_status and http_status >= 500),
        ) from exc

    if result.get("ok"):
        return result.get("result")

    error_code = result.get("error_code", http_status)
    parameters = result.get("parameters") or {}
    retry_after = parameters.get("retry_after")
    description = result.get("description", "Unknown Bot API error")
    retryable = error_code == 429 or bool(error_code and error_code >= 500)
    raise BotAPIError(description, error_code, retry_after, retryable)


def _bot_api_request(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read().decode("utf-8")
            return _parse_bot_response(body, response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return _parse_bot_response(body, exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BotAPIError(str(exc), retryable=True) from exc


async def bot_api_request(method: str, payload: dict, attempts: int = 5):
    delays = (1, 2, 4, 8, 12)
    for attempt in range(attempts):
        try:
            return await asyncio.to_thread(_bot_api_request, method, payload)
        except BotAPIError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise

            if exc.error_code == 429:
                delay = exc.retry_after
                # Avoid holding a delivery worker for a very long flood wait.
                if delay is None or delay > 60:
                    raise
            else:
                delay = delays[attempt]

            delay = max(1, int(delay))
            log.warning(
                "Temporary Bot API failure for %s (attempt %s/%s): %s; retrying in %ss",
                method, attempt + 1, attempts, exc, delay,
            )
            await asyncio.sleep(delay)


async def send_from_bot(method: str, payload: dict, label: str):
    log.info("BOT API START: %s using %s", label, method)
    result = await bot_api_request(method, payload)
    log.info("BOT API SUCCESS: %s sent via bot", label)
    return result


async def deliver_opted_in_user(user_id: int, first_name: str) -> None:
    if user_id in in_progress:
        log.info("Delivery already running for user_id=%s", user_id)
        return

    in_progress.add(user_id)
    try:
        async with delivery_semaphore:
            ensure_opt_in_record(user_id)
            row = get_request(user_id)
            if not row:
                raise RuntimeError("Could not load delivery queue record")

            stage = int(row["stage"])
            if stage >= 4 or row["status"] == "delivered":
                log.info("User %s has already received the full sequence", user_id)
                return

            log.info(
                "OPT-IN DELIVERY START user_id=%s stage=%s source=%s",
                user_id, stage, row["source"],
            )

            if stage < 1:
                welcome_payload = {
                    "chat_id": user_id,
                    "text": WELCOME_TEXT.replace("{name}", first_name),
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                }
                await send_from_bot("sendMessage", welcome_payload, "welcome text")
                save_stage(user_id, 1)
                stage = 1

            if stage < 2:
                video_chat, video_message_id = parse_telegram_post_url(VIDEO_POST_URL)
                video_payload = {
                    "chat_id": user_id,
                    "from_chat_id": video_chat,
                    "message_id": video_message_id,
                }
                # copyMessage keeps the original media caption and formatting.
                await send_from_bot("copyMessage", video_payload, "setup video")
                save_stage(user_id, 2)
                stage = 2

            if stage < 3:
                apk_chat, apk_message_id = parse_telegram_post_url(APK_POST_URL)
                apk_payload = {
                    "chat_id": user_id,
                    "from_chat_id": apk_chat,
                    "message_id": apk_message_id,
                    "reply_markup": APK_BUTTONS,
                }
                # copyMessage keeps the APK post's original caption and formatting.
                await send_from_bot("copyMessage", apk_payload, "APK message")
                save_stage(user_id, 3)
                stage = 3

            if stage < 4:
                await send_from_bot(
                    "sendMessage",
                    {
                        "chat_id": user_id,
                        "text": INTRO_MESSAGE,
                        "link_preview_options": {"is_disabled": True},
                    },
                    "intro message",
                )
                save_stage(user_id, 4)

            log.info("DELIVERY COMPLETE user_id=%s", user_id)

    except Exception as exc:
        save_error(user_id, str(exc))
        log.exception(
            "OPT-IN DELIVERY FAILED user_id=%s; progress saved. User can send /retry.",
            user_id,
        )
        raise
    finally:
        in_progress.discard(user_id)


@bot.on(events.Raw(types.UpdateBotChatInviteRequester))
async def on_join_request(update):
    """Persist join requests only; do not send unsolicited private messages."""
    user_id = int(update.user_id)
    peer = str(update.peer)

    try:
        save_join_request(user_id, peer)
        row = get_request(user_id)
        log.info(
            "JOIN REQUEST QUEUED user_id=%s peer=%s requests=%s status=%s; waiting for /start",
            user_id,
            peer,
            row["request_count"] if row else "?",
            row["status"] if row else "?",
        )
    except Exception:
        log.exception("Could not persist join request for user_id=%s", user_id)


@bot.on(events.NewMessage(
    incoming=True,
    pattern=r"^/(start|retry)(?:@\w+)?(?:\s|$)",
))
async def on_start_or_retry(event):
    if not event.is_private:
        return

    user_id = event.sender_id
    if not user_id:
        return

    command = (event.raw_text or "").split(maxsplit=1)[0].split("@", 1)[0].lower()

    try:
        sender = await event.get_sender()
        first_name = html.escape((getattr(sender, "first_name", None) or "").strip() or "DOST")
    except Exception:
        first_name = "DOST"

    if command == "/retry":
        existing = get_request(user_id)
        if not existing:
            await send_from_bot(
                "sendMessage",
                {
                    "chat_id": user_id,
                    "text": "No previous delivery was found. Send /start to begin.",
                },
                "retry help",
            )
            return

    try:
        await deliver_opted_in_user(user_id, first_name)
        latest = get_request(user_id)
        if latest and latest["status"] == "delivered":
            await send_from_bot(
                "sendMessage",
                {
                    "chat_id": user_id,
                    "text": "✅ All messages have been delivered.",
                },
                "delivery confirmation",
            )
    except Exception:
        # The request and last completed stage remain in SQLite.
        # The user can send /retry after a temporary failure.
        try:
            await send_from_bot(
                "sendMessage",
                {
                    "chat_id": user_id,
                    "text": "Delivery was interrupted. Your progress is saved. Please send /retry.",
                },
                "retry notice",
            )
        except Exception:
            log.exception("Could not send retry notice to user_id=%s", user_id)


async def main():
    init_db()
    await bot.start(bot_token=BOT_TOKEN)

    me_bot = await bot.get_me()
    log.info(
        "Running. Bot: @%s | durable queue: %s",
        me_bot.username or "unknown",
        DB_PATH,
    )
    log.info(
        "Join requests are queued. Users receive messages only after they open the bot and send /start."
    )

    await bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by operator.")
