import asyncio
import html
import http.client
import json
import logging
import mimetypes
import os
import tempfile
import urllib.error
import urllib.request

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserIsBlockedError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
STRING_SESSION = os.environ["STRING_SESSION"]

VIDEO_POST_URL = "https://t.me/HARSHUREQASSETS/4"
APK_POST_URL = "https://t.me/HARSHUREQASSETS/3"

# Sent first by the account logged in through STRING_SESSION.
INTRO_MESSAGE = (
    'KITNE RUPEES KA LOSS HUA TUMHARA RECOVER KRNA CHAHTE HO?'
)

# Bot message 1: welcome text. {name} is replaced with the requester's first name.
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

# Video and APK are copied as-is from the channel posts, so their original
# captions (with custom emojis) come along automatically. No caption is set here.

# Inline buttons under the APK message.
# style: "danger" = red, "success" = green, "primary" = blue.
APK_BUTTONS = {
    "inline_keyboard": [
        [{
            "text": "DM FOR LOSS RECOVERY",
            "url": "https://t.me/m/ymJvbzaFZjg1",
            "icon_custom_emoji_id": "5465476962266934316",
            "style": "danger",
        }],
        [{
            "text": "REGISTRATION LINK",
            "url": "https://www.dmplay20.com/#/register?invitationCode=33981150040",
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

# All bot messages go through the Bot API only.
# There is deliberately no STRING_SESSION fallback for bot messages.
# No join request is approved or declined here.

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    force=True,
)
# Show application logs and Telethon's INFO/WARNING/ERROR logs in the same terminal/journal.
logging.getLogger("telethon").setLevel(logging.INFO)
log = logging.getLogger("joinbot")

# Bot listens for join-request updates. The user account is the reliable sender
# when a requester has not started the bot in a private chat.
bot = TelegramClient(StringSession(), API_ID, API_HASH)
user = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

already_messaged: set[int] = set()
in_progress: set[int] = set()


class BotAPIError(RuntimeError):
    """Raised when Telegram's HTTP Bot API returns ok=false."""


def _bot_api_request(method: str, payload: dict):
    """Synchronous Bot API request, called through asyncio.to_thread()."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            raise BotAPIError(f"HTTP {exc.code}: {body}") from exc
        if not result.get("ok"):
            raise BotAPIError(result.get("description", f"HTTP {exc.code}")) from exc
        return result.get("result")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BotAPIError(str(exc)) from exc

    if not result.get("ok"):
        raise BotAPIError(result.get("description", "Unknown Bot API error"))
    return result.get("result")


async def bot_api_request(method: str, payload: dict):
    return await asyncio.to_thread(_bot_api_request, method, payload)


async def resolve_user(user_id: int, peer):
    """Resolve the requester using the STRING_SESSION account's own access hash."""
    try:
        return await user.get_input_entity(user_id)
    except ValueError:
        pass

    try:
        chat = await user.get_input_entity(peer)
        result = await user(
            functions.messages.GetChatInviteImportersRequest(
                peer=chat,
                offset_date=0,
                offset_user=types.InputUserEmpty(),
                limit=100,
                requested=True,
            )
        )
        for requester in result.users:
            if requester.id == user_id:
                return utils.get_input_user(requester)
    except Exception as exc:
        log.warning("Could not fetch pending requesters: %s", exc)

    return None


async def send_intro_from_user_session(target):
    """The STRING_SESSION account sends exactly one message: the intro."""
    log.info("STEP 1/4 START: sending intro via user session")
    try:
        await user.send_message(target, INTRO_MESSAGE, parse_mode="html", link_preview=False)
        log.info("STEP 1/4 SUCCESS: intro sent via user session")
    except Exception:
        log.exception("STEP 1/4 FAILED: intro could not be sent via user session")
        raise


async def send_from_bot(method: str, payload: dict, label: str):
    """Send a follow-up only through the bot. Never fall back to STRING_SESSION."""
    log.info("BOT API START: %s using %s", label, method)
    try:
        result = await bot_api_request(method, payload)
        log.info("BOT API SUCCESS: %s sent via bot", label)
        return result
    except Exception:
        log.exception("BOT API FAILED: %s using %s", label, method)
        raise


def _telegram_entities_to_bot_api(entities):
    """Convert supported Telethon caption entities to Bot API caption_entities."""
    entity_types = {
        "MessageEntityBold": "bold",
        "MessageEntityItalic": "italic",
        "MessageEntityUnderline": "underline",
        "MessageEntityStrike": "strikethrough",
        "MessageEntityCode": "code",
        "MessageEntityPre": "pre",
        "MessageEntityTextUrl": "text_link",
        "MessageEntityUrl": "url",
        "MessageEntityEmail": "email",
        "MessageEntityPhone": "phone_number",
        "MessageEntityMention": "mention",
        "MessageEntityHashtag": "hashtag",
        "MessageEntityCashtag": "cashtag",
        "MessageEntityBotCommand": "bot_command",
        "MessageEntityCustomEmoji": "custom_emoji",
        "MessageEntitySpoiler": "spoiler",
        "MessageEntityBlockquote": "blockquote",
    }
    converted = []
    for entity in entities or []:
        entity_type = entity_types.get(type(entity).__name__)
        if not entity_type:
            continue
        item = {
            "type": entity_type,
            "offset": entity.offset,
            "length": entity.length,
        }
        if entity_type == "text_link":
            item["url"] = entity.url
        elif entity_type == "custom_emoji":
            item["custom_emoji_id"] = str(entity.document_id)
        elif entity_type == "pre" and getattr(entity, "language", None):
            item["language"] = entity.language
        converted.append(item)
    return converted


def _bot_api_upload_file(method: str, payload: dict, field_name: str,
                         file_path: str, filename: str, content_type: str):
    """Stream a local media file to Telegram Bot API using multipart/form-data."""
    boundary = "----HarshuReqBot" + os.urandom(12).hex()
    chunks = []

    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        else:
            value = str(value)
        chunks.append(
            f"--{boundary}\\r\\n"
            f'Content-Disposition: form-data; name="{key}"\\r\\n'
            f"\\r\\n{value}\\r\\n"
        .encode("utf-8")
        )

    safe_filename = os.path.basename(filename).replace('"', "_").replace("\\r", "").replace("\\n", "")
    file_header = (
        f"--{boundary}\\r\\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_filename}"\\r\\n'
        f"Content-Type: {content_type}\\r\\n\\r\\n"
    ).encode("utf-8")
    closing = f"\\r\\n--{boundary}--\\r\\n".encode("utf-8")
    prefix = b"".join(chunks) + file_header
    content_length = len(prefix) + os.path.getsize(file_path) + len(closing)

    connection = http.client.HTTPSConnection("api.telegram.org", timeout=180)
    try:
        connection.putrequest("POST", f"/bot{BOT_TOKEN}/{method}")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.putheader("Connection", "close")
        connection.endheaders()

        connection.send(prefix)
        with open(file_path, "rb") as media_file:
            while True:
                data = media_file.read(1024 * 1024)
                if not data:
                    break
                connection.send(data)
        connection.send(closing)

        response = connection.getresponse()
        raw_result = response.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(raw_result)
        except json.JSONDecodeError as exc:
            raise BotAPIError(
                f"Telegram returned HTTP {response.status}: {raw_result[:500]}"
            ) from exc

        if not result.get("ok"):
            raise BotAPIError(result.get("description", f"HTTP {response.status}"))
        return result.get("result")
    finally:
        connection.close()


async def send_source_media_from_bot(method: str, payload: dict, source_post_url: str,
                                     field_name: str, label: str):
    """Download a channel post with the user session, then upload it via Bot API sendVideo/sendDocument."""
    source_chat_name, source_message_id = parse_telegram_post_url(source_post_url)
    temp_path = None
    log.info("BOT API START: %s using %s", label, method)

    try:
        source_chat = await user.get_entity(source_chat_name)
        source_message = await user.get_messages(source_chat, ids=source_message_id)
        if source_message is None or not source_message.media:
            raise RuntimeError(f"Source post has no downloadable media: {source_post_url}")

        original_name = getattr(getattr(source_message, "file", None), "name", None)
        if not original_name:
            original_name = "video.mp4" if method == "sendVideo" else "attachment.apk"

        suffix = os.path.splitext(original_name)[1]
        if not suffix:
            suffix = ".mp4" if method == "sendVideo" else ".apk"

        with tempfile.NamedTemporaryFile(prefix="harshureqbot_", suffix=suffix, delete=False) as temp_file:
            temp_path = temp_file.name

        downloaded_path = await user.download_media(source_message, file=temp_path)
        if not downloaded_path or not os.path.isfile(downloaded_path):
            raise RuntimeError(f"Could not download source media: {source_post_url}")

        upload_payload = dict(payload)
        original_caption = source_message.message or ""
        if original_caption:
            upload_payload["caption"] = original_caption
            caption_entities = _telegram_entities_to_bot_api(source_message.entities)
            if caption_entities:
                upload_payload["caption_entities"] = caption_entities

        content_type = mimetypes.guess_type(original_name)[0]
        if not content_type:
            content_type = "video/mp4" if method == "sendVideo" else "application/vnd.android.package-archive"

        result = await asyncio.to_thread(
            _bot_api_upload_file,
            method,
            upload_payload,
            field_name,
            downloaded_path,
            original_name,
            content_type,
        )
        log.info("BOT API SUCCESS: %s sent via bot", label)
        return result
    except Exception:
        log.exception("BOT API FAILED: %s using %s", label, method)
        raise
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                log.warning("Could not remove temporary media file: %s", temp_path)


def parse_telegram_post_url(post_url: str):
    """Parse a public Telegram post URL into source chat and message ID."""
    from urllib.parse import urlparse
    parsed = urlparse(post_url)
    parts = parsed.path.strip("/").split("/")
    if parsed.netloc not in {"t.me", "www.t.me"} or len(parts) != 2 or not parts[1].isdigit():
        raise ValueError(f"Unsupported Telegram post URL: {post_url}")
    return f"@{parts[0]}", int(parts[1])


async def get_first_name(user_id: int, target) -> str:
    """Requester's first name (HTML-escaped). Falls back to a generic word."""
    for ref in (target, user_id):
        try:
            entity = await user.get_entity(ref)
            name = (getattr(entity, "first_name", None) or "").strip()
            if name:
                return html.escape(name)
        except Exception as exc:
            log.warning("Could not fetch name via %r: %s", type(ref).__name__, exc)
    return "DOST"


async def send_sequence(user_id: int, target):
    log.info("MESSAGE SEQUENCE START for user_id=%s", user_id)

    bot_messages_sent = False

    # 1) Bot sends the welcome text first.
    try:
        first_name = await get_first_name(user_id, target)
        welcome_payload = {
            "chat_id": user_id,
            "text": WELCOME_TEXT.replace("{name}", first_name),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        await send_from_bot("sendMessage", welcome_payload, "welcome text")

        # 2) Bot sends the video as a new upload, keeping its original caption.
        video_payload = {"chat_id": user_id}
        await send_source_media_from_bot(
            "sendVideo", video_payload, VIDEO_POST_URL, "video", "setup video"
        )

        # 3) Bot sends the APK as a new document upload, keeping its original caption and buttons.
        apk_payload = {
            "chat_id": user_id,
            "reply_markup": APK_BUTTONS,
        }
        await send_source_media_from_bot(
            "sendDocument", apk_payload, APK_POST_URL, "document", "APK message"
        )

        bot_messages_sent = True
        log.info("BOT MESSAGE SEQUENCE COMPLETE for user_id=%s", user_id)

    except Exception:
        # A bot cannot initiate a private chat until the user has opened it and
        # pressed /start. Keep the requested order, but do not block the userbot
        # intro when the Bot API rejects its private message.
        log.exception(
            "BOT MESSAGE SEQUENCE FAILED for user_id=%s; continuing to userbot intro",
            user_id,
        )

    # 4) User session sends the intro only after the bot stage was attempted.
    await send_intro_from_user_session(target)

    if bot_messages_sent:
        log.info("FULL MESSAGE SEQUENCE COMPLETE for user_id=%s", user_id)
    else:
        log.warning(
            "USERBOT INTRO SENT, BUT BOT MESSAGES WERE NOT DELIVERED for user_id=%s. "
            "The requester may need to open the bot and press /start.",
            user_id,
        )


@bot.on(events.Raw(types.UpdateBotChatInviteRequester))
async def on_join_request(update):
    user_id = update.user_id

    if user_id in already_messaged or user_id in in_progress:
        return
    in_progress.add(user_id)

    log.info("Join request from %s in %s", user_id, update.peer)

    try:
        target = await resolve_user(user_id, update.peer)
        if target is None:
            log.warning(
                "Cannot resolve user %s. Make sure the string-session account is an "
                "admin of that chat and can see its pending join requests.",
                user_id,
            )
            return

        await send_sequence(user_id, target)
        already_messaged.add(user_id)
        log.info("Full message sequence sent to %s", user_id)

    except FloodWaitError as exc:
        # Do not queue delayed sends; log Telegram's required wait and exit this sequence.
        log.warning("FloodWait: Telegram requires a %ss wait; this sequence will stop without retrying.", exc.seconds)
    except (UserPrivacyRestrictedError, UserIsBlockedError):
        log.info("User %s does not accept private messages", user_id)
    except PeerFloodError:
        log.error("PeerFlood: the user account is limited for messaging new users. Stop sending and wait.")
    except Exception as exc:
        log.exception("Message sequence failed for %s: %s", user_id, exc)
    finally:
        in_progress.discard(user_id)


async def main():
    await user.connect()
    if not await user.is_user_authorized():
        raise SystemExit("STRING_SESSION is invalid or revoked.")

    await bot.start(bot_token=BOT_TOKEN)
    await user.get_dialogs()

    me_user = await user.get_me()
    me_bot = await bot.get_me()
    log.info(
        "Running. Bot: @%s | Sender account: %s",
        me_bot.username,
        me_user.id,
    )
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
