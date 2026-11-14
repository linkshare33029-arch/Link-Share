import asyncio
import contextvars
import html
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeChat, ChatAdministratorRights, CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, KeyboardButtonRequestChat, LinkPreviewOptions, Message, ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from pymongo import AsyncMongoClient
from about import render_about

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("link-share")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN must be set in Render environment variables.")
def load_owner_ids() -> set[int]:
    """Load all owners from the supported OWNER_ID environment variables."""
    values = []
    for name in ("OWNER_ID", "OWNER_IDS"):
        value = os.environ.get(name, "").strip()
        if value:
            values.append((name, value))

    for name, value in os.environ.items():
        if name.startswith("OWNER_ID_") and name[len("OWNER_ID_"):].isdigit():
            if value.strip():
                values.append((name, value.strip()))

    owner_ids = set()
    for name, raw_value in values:
        for item in raw_value.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                owner_id = int(item)
            except ValueError as exc:
                raise RuntimeError(
                    f"{name} must contain only comma-separated Telegram user IDs."
                ) from exc
            if owner_id <= 0:
                raise RuntimeError(f"{name} contains an invalid Telegram user ID: {item}")
            owner_ids.add(owner_id)

    if not owner_ids:
        raise RuntimeError(
            "Set at least one owner using OWNER_ID, OWNER_IDS, or OWNER_ID_2 (and so on)."
        )
    return owner_ids


OWNER_IDS = load_owner_ids()
MONGO_URI = os.environ.get("URI", "").strip()
DB_NAME = os.environ.get("DB_NAME", "link_share").strip() or "link_share"
if not MONGO_URI:
    raise RuntimeError("URI must be set in Render environment variables.")

mongo = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=20000, connectTimeoutMS=20000)
default_db = None
current_db = contextvars.ContextVar("current_db", default=None)
bot_databases = {}

class CollectionProxy:
    def __init__(self, name):
        self.name = name
    def __getattr__(self, attr):
        active_db = current_db.get()
        if active_db is None:
            active_db = default_db
        if active_db is None:
            raise RuntimeError("No bot database is available in the current context.")
        return getattr(active_db[self.name], attr)

chats = CollectionProxy("chats")
settings = CollectionProxy("settings")
issued = CollectionProxy("issued_links")
ui_state = CollectionProxy("owner_ui")
users = CollectionProxy("users")

START_CONFIG_ID = "start_message"
DEFAULT_START_CONFIG = {
    "_id": START_CONFIG_ID, "enabled": True, "text": "", "about_enabled": True,
    "media_type": None, "media_file_id": None,
    "button1_text": "", "button1_url": "", "button2_text": "", "button2_url": "",
    "button3_text": "", "button3_url": "", "button4_text": "", "button4_url": "",
}

async def track_user_id(user_id: int):
    """Keep every interacting Telegram user ID in one comma-separated string field."""
    value = str(int(user_id))
    await users.update_one(
        {"_id": "all"},
        [{"$set": {"id": {"$let": {
            "vars": {"current": {"$ifNull": ["$id", ""]}, "new_id": value},
            "in": {"$cond": [
                {"$in": ["$$new_id", {"$split": [{"$ifNull": ["$id", ""]}, ", "]}]},
                "$$current",
                {"$cond": [
                    {"$eq": ["$$current", ""]}, "$$new_id",
                    {"$concat": ["$$current", ", ", "$$new_id"]}
                ]}
            ]}
        }}}}],
        upsert=True,
    )

async def read_start_config():
    """Read Start settings without creating or changing any database records."""
    config = await settings.find_one({"_id": START_CONFIG_ID})
    return config or DEFAULT_START_CONFIG.copy()


DEFAULT_START_TEXT = (
    "<i>✨ Welcome to <b>{bot_mention}</b>!\n\n"
    "Choose an option below to explore the available communities and get a fresh invite link.</i>"
)


async def cleanup_empty_start_config():
    absent = {key: {"$exists": False} for key in (
        "text", "media_type", "media_file_id",
        "button1_text", "button1_url", "button2_text", "button2_url",
        "button3_text", "button3_url", "button4_text", "button4_url",
    )}
    await settings.delete_one({"_id": START_CONFIG_ID, **absent})


async def get_start_config():
    # Defaults stay in source; MongoDB stores only manually configured overrides.
    config = await settings.find_one({"_id": START_CONFIG_ID})
    return {**DEFAULT_START_CONFIG, **(config or {})}


def render_start_text(config, bot_info=None, user=None):
    value = (config.get("text") or "").strip()
    if not value:
        if bot_info is None:
            value = "<i>✨ Welcome! Choose an option below to continue.</i>"
        else:
            name = html.escape(bot_info.full_name)
            username = html.escape(bot_info.username, quote=True) if bot_info.username else ""
            mention = f'<a href="https://t.me/{username}">{name}</a>' if username else name
            value = DEFAULT_START_TEXT.format(bot_mention=mention)
    if bot_info is None:
        return value
    bot_name = html.escape(bot_info.full_name)
    username = html.escape(bot_info.username, quote=True) if bot_info.username else ""
    bot_mention = f'<a href="https://t.me/{username}">{bot_name}</a>' if username else bot_name
    anime = '<a href="http://t.me/lexianime">Lexi Anime</a>'
    user_name = html.escape(user.full_name) if user else ""
    mention = (
        f'<a href="tg://user?id={user.id}">{user_name}</a>'
        if user else user_name
    )
    return (value.replace("{bot_name}", bot_name)
                 .replace("{bot_mention}", bot_mention)
                 .replace("{user_name}", user_name)
                 .replace("{mention}", mention)
                 .replace("{anime}", anime))


def telegram_html_length(value: str) -> int:
    # Telegram applies its limits to rendered text, not HTML tag syntax.
    plain = html.unescape(re.sub(r"<[^>]*>", "", value))
    return telegram_text_length(plain)

def start_config_keyboard(config):
    # Keep the four configured slots in their fixed two-row positions.
    rows = []
    for first, second in ((1, 2), (3, 4)):
        row = []
        for index in (first, second):
            label = (config.get(f"button{index}_text") or "").strip()
            url = (config.get(f"button{index}_url") or "").strip()
            parsed = urlparse(url)
            if label and parsed.scheme in {"http", "https"} and parsed.netloc:
                row.append(InlineKeyboardButton(text=label[:64], url=url))
        if row:
            rows.append(row)
    # About is displayed only while enabled in owner settings.
    if config.get("about_enabled", True):
        rows.append([InlineKeyboardButton(text=small_caps_label("About"), callback_data="start:about")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def send_start_page(message: Message):
    config = await get_start_config()
    if not config.get("enabled", True):
        await message.answer("This page is currently unavailable.")
        return
    body = render_start_text(config, await get_bot_info(), message.from_user)
    markup = start_config_keyboard(config)
    media_id, media_type = config.get("media_file_id"), config.get("media_type")
    try:
        if media_id and media_type == "photo":
            if telegram_html_length(body) <= 1024:
                await message.answer_photo(media_id, caption=body, reply_markup=markup)
            else:
                await message.answer_photo(media_id, reply_markup=markup)
                await message.answer(body, link_preview_options=LinkPreviewOptions(is_disabled=True))
        elif media_id and media_type == "animation":
            if telegram_html_length(body) <= 1024:
                await message.answer_animation(media_id, caption=body, reply_markup=markup)
            else:
                await message.answer_animation(media_id, reply_markup=markup)
                await message.answer(body, link_preview_options=LinkPreviewOptions(is_disabled=True))
        else:
            await message.answer(body, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramAPIError:
        log.exception("Could not send configured Start page; using text fallback")
        await message.answer(
            body, reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

def small_caps_label(value: str) -> str:
    lower = "abcdefghijklmnopqrstuvwxyz"
    small = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
    table = str.maketrans(lower + lower.upper(), small + small)
    return value.translate(table)


def start_settings_keyboard(config, back_callback=None):
    rows = [
        [InlineKeyboardButton(text=small_caps_label("Caption"), callback_data="startcfg:caption"),
         InlineKeyboardButton(text=small_caps_label("Media"), callback_data="startcfg:media")],
        [InlineKeyboardButton(text=small_caps_label("Buttons"), callback_data="startcfg:buttons"),
         InlineKeyboardButton(text=small_caps_label("About"), callback_data="startcfg:about_toggle")],
    ]
    if back_callback:
        rows.append([InlineKeyboardButton(text=small_caps_label("Back"), callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_settings_body(config):
    caption_on = bool((config.get("text") or "").strip())
    media_on = bool(config.get("media_file_id"))
    about_on = config.get("about_enabled", True)
    return (
        "<b><u>START MESSAGE</u></b>\n\n"
        "<i>Customize your bot's start message using the following options.</i>\n\n"
        f"<i>Caption: {'Added ✅' if caption_on else 'Default ✨'}</i>\n"
        f"<i>Media: {'Added ✅' if media_on else 'None ❌'}</i>\n"
        f"<i>About: {'Enabled ✅' if about_on else 'Disabled ❌'}</i>"
    )


def start_submenu_keyboard(kind, config):
    if kind == "caption":
        has_value = bool((config.get("text") or "").strip())
        first = InlineKeyboardButton(
            text=small_caps_label("Edit") if has_value else "✚ " + small_caps_label("Add"),
            callback_data="startcfg:caption_edit",
        )
        rows = [[first, InlineKeyboardButton(text=small_caps_label("See"), callback_data="startcfg:caption_show")],
                [InlineKeyboardButton(text=small_caps_label("Default"), callback_data="startcfg:caption_default")]]
    else:
        has_value = bool(config.get("media_file_id"))
        first = InlineKeyboardButton(
            text="✚ " + small_caps_label("Add"),
            callback_data="startcfg:media_edit",
        )
        rows = [[first]]
        if has_value:
            rows[0].append(InlineKeyboardButton(text=small_caps_label("Remove"), callback_data="startcfg:media_remove"))
    rows.append([InlineKeyboardButton(text="❮❮ " + small_caps_label("Back"), callback_data="startcfg:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_buttons_body(config):
    lines = ["<b><u>START BUTTONS</u></b>", "", "<i>Customize buttons shown with your start message.</i>", ""]
    for index in range(1, 5):
        enabled = bool((config.get(f"button{index}_text") or "").strip() and (config.get(f"button{index}_url") or "").strip())
        lines.append(f"<i>Button {index}: {'ON ✅' if enabled else 'OFF ❌'}</i>")
    return "\n".join(lines)


def start_buttons_keyboard(config):
    rows = []
    for first, second in ((1, 2), (3, 4)):
        row = []
        for index in (first, second):
            enabled = bool((config.get(f"button{index}_text") or "").strip() and (config.get(f"button{index}_url") or "").strip())
            row.append(InlineKeyboardButton(text=f"{index} {'ON ✅' if enabled else 'OFF ❌'}", callback_data=f"startcfg:button{index}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❮❮ " + small_caps_label("Back"), callback_data="startcfg:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_start_settings(message: Message, back_callback=None):
    config = await get_start_config()
    body, markup = start_settings_body(config), start_settings_keyboard(config, back_callback)
    panel_key = f"settings:{message.chat.id}"
    home_key = f"home:{message.chat.id}"
    existing = await ui_state.find_one({"_id": panel_key})
    if not existing:
        existing = await ui_state.find_one({"_id": home_key})
    target_id = (existing or {}).get("message_id")
    if target_id:
        try:
            await BOT.edit_message_text(
                body, chat_id=message.chat.id, message_id=target_id, reply_markup=markup
            )
            await ui_state.update_one(
                {"_id": panel_key}, {"$set": {"message_id": target_id}}, upsert=True
            )
            await ui_state.delete_one({"_id": home_key})
            return
        except TelegramAPIError:
            log.info("Could not reuse previous owner panel; creating a replacement.")
    sent = await message.answer(body, reply_markup=markup)
    await ui_state.update_one(
        {"_id": panel_key}, {"$set": {"message_id": sent.message_id}}, upsert=True
    )
    await ui_state.delete_one({"_id": home_key})


async def clear_owner_prompt(owner_id: int):
    """Clear pending input state and remove its separate prompt message."""
    pending = await ui_state.find_one({"_id": f"prompt:{owner_id}"})
    await ui_state.delete_one({"_id": f"prompt:{owner_id}"})
    if pending and pending.get("prompt_message_id") and pending.get("panel_chat_id"):
        try:
            await BOT.delete_message(pending["panel_chat_id"], pending["prompt_message_id"])
        except TelegramAPIError:
            pass

def telegram_text_length(value: str) -> int:
    """Count text as UTF-16 code units, matching Telegram's entity length rules."""
    return len(value.encode("utf-16-le")) // 2


DEFAULT_CAPTION = "<b><i>» Hᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ!</i></b>\n<b>» ᴄʟɪᴄᴋ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴘʀᴏᴄᴇᴇᴅ:</b>"
DEFAULT_BUTTON = "• ᴄʟɪᴄᴋ ʜᴇʀᴇ •"
class ActiveBotProxy:
    """Resolve Telegram API calls to the bot handling the current update/task."""
    def __getattr__(self, name):
        active = current_bot.get() or default_bot
        if active is None:
            raise RuntimeError("No active Telegram bot is available.")
        return getattr(active, name)


default_bot = None
bot_info_cache = None


async def get_bot_info():
    return bot_info_cache or await BOT.get_me()


current_bot = contextvars.ContextVar("current_bot", default=None)
current_bot_username = contextvars.ContextVar("current_bot_username", default="")
current_bot_user_id = contextvars.ContextVar("current_bot_user_id", default=0)
bot_registry = {}
BOT = ActiveBotProxy()
dp = Dispatcher()
expiry_tasks = {}
POST_CONFIG_CACHE = {}
broadcast_tasks = {}


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def small_caps(value: str) -> str:
    return value.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    ))


HOME_PAGE_SIZE = 20


def main_keyboard(items, page=0):
    total_pages = max(1, (len(items) + HOME_PAGE_SIZE - 1) // HOME_PAGE_SIZE)
    page = max(0, min(int(page), total_pages - 1))
    start = page * HOME_PAGE_SIZE
    visible = items[start:start + HOME_PAGE_SIZE]
    rows, row = [], []
    for item in visible:
        row.append(InlineKeyboardButton(
            text=small_caps_label(item["title"][:64]),
            callback_data=f"chat:{item['code']}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text=small_caps_label("Previous"), callback_data=f"home:page:{page - 1}"
            ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text=small_caps_label("Next"), callback_data=f"home:page:{page + 1}"
            ))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="+", callback_data="add")])
    rows.append([InlineKeyboardButton(text=small_caps_label("Links Post"), callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps_label("Channel"), callback_data="pick:channel"),
         InlineKeyboardButton(text=small_caps_label("Group"), callback_data="pick:group")],
        [InlineKeyboardButton(text=small_caps_label("Back"), callback_data="home")],
    ])


def settings_keyboard(back_callback="home"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ᴅᴇꜰᴀᴜʟᴛ", callback_data="cfg:default"),
         InlineKeyboardButton(text="ᴍᴇᴅɪᴀ", callback_data="cfg:media")],
        [InlineKeyboardButton(text="ᴄᴀᴘᴛɪᴏɴ", callback_data="cfg:caption"),
         InlineKeyboardButton(text="ʙᴜᴛᴛᴏɴ", callback_data="cfg:button")],
        [InlineKeyboardButton(text=small_caps_label("Back"), callback_data=back_callback)],
    ])


def detail_keyboard(code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps_label("Replace Chat"), callback_data=f"replace:{code}")],
        [InlineKeyboardButton(text=small_caps_label("Back"), callback_data="home")],
    ])


async def get_config():
    cache_key = current_bot_user_id.get()
    if cache_key in POST_CONFIG_CACHE:
        return POST_CONFIG_CACHE[cache_key]
    defaults = {
        "_id": "post", "caption": DEFAULT_CAPTION, "caption_entities": [],
        "button": DEFAULT_BUTTON, "media_type": None, "media_file_id": None,
    }
    config = await settings.find_one({"_id": "post"})
    old_default_caption = "<blockquote><b><i>» Hᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ!</i></b></blockquote><b>» ᴄʟɪᴄᴋ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴘʀᴏᴄᴇᴇᴅ:</b>"
    if config and config.get("caption") == old_default_caption:
        config = {**config, "caption": DEFAULT_CAPTION, "caption_entities": []}
    # An untouched default is served from code and is never persisted.
    POST_CONFIG_CACHE[cache_key] = config or defaults
    return POST_CONFIG_CACHE[cache_key]


def invalidate_config_cache():
    POST_CONFIG_CACHE.pop(current_bot_user_id.get(), None)


async def home_text():
    return ("<b><u>ROTATION LINKS CHATS</u></b>\n\n"
            "<i>Select any chat for links delivery. A new link will be used every time to avoid spammers.</i>")


async def clear_owner_panels(chat_id: int, owner_id: int):
    # Read and clear independent UI state concurrently to reduce command latency.
    keys = [f"home:{chat_id}", f"settings:{chat_id}", f"commands:{chat_id}", f"picker:{owner_id}", f"prompt:{owner_id}"]
    saved = await asyncio.gather(*(ui_state.find_one({"_id": key}) for key in keys))
    old_panel_ids = [
        item["message_id"] for item in saved[:3]
        if item and item.get("message_id")
    ]
    await asyncio.gather(*(ui_state.delete_one({"_id": key}) for key in keys))
    picker = saved[3]
    if picker and picker.get("prompt_message_id"):
        try:
            await BOT.delete_message(chat_id, picker["prompt_message_id"])
        except TelegramAPIError:
            pass
    old_prompt = saved[4]
    if old_prompt and old_prompt.get("prompt_message_id") and old_prompt.get("panel_chat_id"):
        try:
            await BOT.delete_message(old_prompt["panel_chat_id"], old_prompt["prompt_message_id"])
        except TelegramAPIError:
            pass
    return old_panel_ids


async def finish_owner_command(message: Message, old_panel_ids, keep_panel_id=None):
    # The fresh panel is already visible; now remove the command and previous panel(s).
    delete_ids = [old_id for old_id in old_panel_ids if old_id != keep_panel_id]
    async def safe_delete(message_id):
        try:
            await BOT.delete_message(message.chat.id, message_id)
        except TelegramAPIError:
            pass
    async def safe_delete_command():
        try:
            await message.delete()
        except TelegramAPIError:
            pass
    await asyncio.gather(
        *(safe_delete(message_id) for message_id in delete_ids),
        safe_delete_command(),
    )

async def sync_chat_titles():
    async for item in chats.find({"active": True}):
        try:
            current = await BOT.get_chat(item["chat_id"])
            title = current.title or item.get("title") or str(item["chat_id"])
            username = getattr(current, "username", None)
            await chats.update_one(
                {"_id": item["_id"]},
                {"$set": {"title": title, "username": username, "is_private": not bool(username)}},
            )
        except TelegramAPIError as exc:
            log.warning("Could not refresh title for chat %s: %s", item.get("chat_id"), exc)


async def refresh_home_panels():
    items = await chats.find({"active": True}).sort("created_at", 1).to_list(length=None)
    async for panel in ui_state.find({"_id": {"$regex": "^home:"}}):
        markup = main_keyboard(items, panel.get("page", 0))
        try:
            chat_id = int(panel["_id"].split(":", 1)[1])
            await BOT.edit_message_text(
                await home_text(), chat_id=chat_id,
                message_id=panel["message_id"], reply_markup=markup,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                log.warning("Could not refresh owner home panel %s: %s", panel.get("_id"), exc)
        except (TelegramAPIError, ValueError, KeyError):
            log.exception("Could not update an owner home panel")


async def render_home(message: Message, edit_existing=False):
    items = await chats.find({"active": True}).sort("created_at", 1).to_list(length=None)
    body = await home_text()
    panel = await ui_state.find_one({"_id": f"home:{message.chat.id}"})
    page = (panel or {}).get("page", 0)
    markup = main_keyboard(items, page)
    target_id = message.message_id if edit_existing else (panel or {}).get("message_id")
    if target_id:
        try:
            await BOT.edit_message_text(body, chat_id=message.chat.id,
                                        message_id=target_id, reply_markup=markup)
            await ui_state.update_one({"_id": f"home:{message.chat.id}"},
                {"$set": {"message_id": target_id}}, upsert=True)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            log.info("Could not edit saved home panel; sending a new one: %s", exc)
    sent = await message.answer(body, reply_markup=markup)
    await ui_state.update_one({"_id": f"home:{message.chat.id}"},
        {"$set": {"message_id": sent.message_id}}, upsert=True)


async def edit_panel(message: Message, body: str, markup=None):
    try:
        await message.edit_text(body, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.warning("Panel edit failed: %s", exc)


async def show_settings(message: Message, back_callback="home"):
    body = (
        "<b><i>LINKS POST</i></b>\n\n"
        "<i>Edit caption, media, and button using the buttons below:</i>"
    )
    await edit_panel(message, body, settings_keyboard(back_callback))
    await ui_state.update_one({"_id": f"settings:{message.chat.id}"},
        {"$set": {"message_id": message.message_id}}, upsert=True)


def request_chat_keyboard(kind: str):
    request_id = secrets.randbelow(2_000_000_000) + 1
    button = KeyboardButton(
        text=small_caps_label(f"Choose {kind.title()}"),
        request_chat=KeyboardButtonRequestChat(
            request_id=request_id,
            chat_is_channel=(kind == "channel"),
            request_title=True,
            request_username=True,
        ),
    )
    return ReplyKeyboardMarkup(
        keyboard=[[button]], resize_keyboard=True, one_time_keyboard=True
    )


async def send_fresh_link(message: Message, chat_doc):
    invite = None
    try:
        # Fetch the current chat metadata and cached post settings concurrently.
        live_chat, cfg = await asyncio.gather(
            BOT.get_chat(chat_doc["chat_id"]),
            get_config(),
        )
        live_username = getattr(live_chat, "username", None)
        live_private = not bool(live_username)
        await chats.update_one({"_id": chat_doc["_id"]}, {"$set": {
            "title": live_chat.title or chat_doc.get("title"),
            "username": live_username,
            "is_private": live_private,
        }})
        chat_doc["is_private"] = live_private
        chat_doc["username"] = live_username
        chat_doc["title"] = live_chat.title or chat_doc.get("title")
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        invite_args = {
            "chat_id": chat_doc["chat_id"],
            "expire_date": expires_at,
            "creates_join_request": bool(chat_doc.get("is_private")),
            "name": f"LS-{message.from_user.id}"[:32],
        }
        if not chat_doc.get("is_private"):
            invite_args["member_limit"] = 1
        invite = await BOT.create_chat_invite_link(**invite_args)
        record = {
            "invite_link": invite.invite_link,
            "chat_id": chat_doc["chat_id"],
            "code": chat_doc["code"],
            "chat_title": chat_doc.get("title", str(chat_doc["chat_id"])),
            "user_id": message.from_user.id,
            "bot_id": current_bot_user_id.get(),
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
            "revoked": False,
        }
        result = await issued.insert_one(record)
        record["_id"] = result.inserted_id
        schedule_expiry(record)
        button_text = (cfg.get("button") or DEFAULT_BUTTON)[:64]
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, url=invite.invite_link)
        ]])
        caption = cfg.get("caption", DEFAULT_CAPTION)
        entities = cfg.get("caption_entities") or None
        media_id, media_type = cfg.get("media_file_id"), cfg.get("media_type")
        kwargs = {
            "caption": caption,
            "reply_markup": markup,
        }
        if entities:
            kwargs["caption_entities"] = entities
            kwargs["parse_mode"] = None
        else:
            kwargs["parse_mode"] = ParseMode.HTML
        if media_id and media_type in {"photo", "animation"} and telegram_text_length(caption) > 1024:
            # Recover safely from legacy/externally edited settings that exceed Telegram's media-caption limit.
            if media_type == "photo":
                await message.answer_photo(media_id)
            else:
                await message.answer_animation(media_id)
            await message.answer(
                caption,
                entities=entities,
                parse_mode=None if entities else ParseMode.HTML,
                reply_markup=markup,
            )
        elif media_id and media_type == "photo":
            await message.answer_photo(media_id, **kwargs)
        elif media_id and media_type == "animation":
            await message.answer_animation(media_id, **kwargs)
        else:
            await message.answer(
                caption,
                entities=entities,
                parse_mode=None if entities else ParseMode.HTML,
                reply_markup=markup,
            )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.exception("Invite creation or link-post delivery failed for chat %s", chat_doc.get("chat_id"))
        if invite:
            try:
                await BOT.revoke_chat_invite_link(chat_doc["chat_id"], invite.invite_link)
            except TelegramAPIError:
                log.exception("Could not revoke an undelivered invite")
        await message.answer(
            "I couldn't create or send the invite. Please check that I'm an administrator "
            "with permission to invite users in that chat."
        )
    except Exception:
        log.exception("Unexpected link delivery error")
        if invite:
            try:
                await BOT.revoke_chat_invite_link(chat_doc["chat_id"], invite.invite_link)
            except TelegramAPIError:
                log.exception("Could not revoke an undelivered invite")
        await message.answer("Something went wrong while creating your link. Please try again later.")


@dp.message(CommandStart())
async def start(message: Message):
    if not message.from_user:
        return
    owner = is_owner(message.from_user.id)
    old_panel_ids = []
    if owner:
        await cancel_broadcast(message.from_user.id, message.chat.id)
        old_panel_ids = await clear_owner_panels(message.chat.id, message.from_user.id)
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if code:
        doc = await chats.find_one({"code": code, "active": True})
        if not doc:
            await message.answer("<i>This link is invalid or no longer available.</i>")
            if owner:
                await finish_owner_command(message, old_panel_ids)
            return
        await send_fresh_link(message, doc)
        if owner:
            await finish_owner_command(message, old_panel_ids)
        return
    await send_start_page(message)
    if owner:
        await finish_owner_command(message, old_panel_ids)

async def cancel_broadcast(owner_id: int, chat_id: int | None = None):
    """Cancel one owner's pending broadcast and remove its prompt."""
    task = broadcast_tasks.pop(owner_id, None)
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    state = await ui_state.find_one({"_id": f"broadcast:{owner_id}"})
    await ui_state.delete_one({"_id": f"broadcast:{owner_id}"})
    if state and state.get("prompt_message_id"):
        try:
            await BOT.delete_message(state.get("chat_id", chat_id), state["prompt_message_id"])
        except (TelegramAPIError, TypeError):
            pass


async def run_broadcast(source_message: Message, chat_id: int, pin: bool):
    users_doc = await users.find_one({"_id": "all"}, {"id": 1})
    saved_ids = (users_doc or {}).get("id", "")
    recipient_ids, seen_ids = [], set()
    for raw_id in saved_ids.split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            user_id = int(raw_id)
        except ValueError:
            log.warning("Ignoring invalid user ID in users collection: %r", raw_id)
            continue
        if user_id > 0 and user_id not in seen_ids:
            seen_ids.add(user_id)
            recipient_ids.append(user_id)
    if not recipient_ids:
        await BOT.send_message(chat_id, "<i>No users are saved in the users collection yet.</i>")
        return

    delivered = failed = pinned = pin_failed = 0
    inline_markup = source_message.reply_markup if isinstance(source_message.reply_markup, InlineKeyboardMarkup) else None
    for user_id in recipient_ids:
        try:
            copied = await source_message.copy_to(chat_id=user_id, reply_markup=inline_markup)
            delivered += 1
            if pin:
                try:
                    await BOT.pin_chat_message(user_id, copied.message_id, disable_notification=True)
                    pinned += 1
                except TelegramAPIError as exc:
                    pin_failed += 1
                    log.info("Broadcast pin failed for user %s: %s", user_id, exc)
        except TelegramAPIError as exc:
            failed += 1
            log.info("Broadcast delivery failed for user %s: %s", user_id, exc)
        except Exception:
            failed += 1
            log.exception("Unexpected broadcast delivery failure for user %s", user_id)
        await asyncio.sleep(0.04)

    result = (
        "<b>✨ Broadcast completed.</b>\n\n"
        f"<b>Users:</b> {len(recipient_ids)}\n"
        f"<b>Delivered:</b> {delivered}\n"
        f"<b>Failed:</b> {failed}"
    )
    if pin:
        result += f"\n<b>Pinned:</b> {pinned}\n<b>Pin failed:</b> {pin_failed}"
    await BOT.send_message(chat_id, result)


async def broadcast_timeout(owner_id: int, chat_id: int):
    try:
        await asyncio.sleep(30)
        if await ui_state.find_one({"_id": f"broadcast:{owner_id}"}):
            await cancel_broadcast(owner_id, chat_id)
            await BOT.send_message(chat_id, "<i>Broadcast process cancelled because no message was received within 30 seconds.</i>")
    except asyncio.CancelledError:
        raise
    finally:
        if broadcast_tasks.get(owner_id) is asyncio.current_task():
            broadcast_tasks.pop(owner_id, None)


async def begin_broadcast(message: Message, pin: bool):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    owner_id, chat_id = message.from_user.id, message.chat.id
    await cancel_broadcast(owner_id, chat_id)
    old_panel_ids = await clear_owner_panels(chat_id, owner_id)
    if message.reply_to_message:
        await run_broadcast(message.reply_to_message, chat_id, pin)
    else:
        prompt = await message.answer("<i>Send or forward the message you want to broadcast. You have 30 seconds.</i>")
        await ui_state.update_one(
            {"_id": f"broadcast:{owner_id}"},
            {"$set": {"chat_id": chat_id, "prompt_message_id": prompt.message_id, "pin": pin}},
            upsert=True,
        )
        broadcast_tasks[owner_id] = asyncio.create_task(broadcast_timeout(owner_id, chat_id))
    await finish_owner_command(message, old_panel_ids)


@dp.message(Command("broadcast"))
async def broadcast_command(message: Message):
    await begin_broadcast(message, pin=False)


@dp.message(Command("pbroadcast"))
async def pinned_broadcast_command(message: Message):
    await begin_broadcast(message, pin=True)


COMMANDS_PAGE_SIZE = 6
OWNER_COMMANDS = [
    ("/start", "Open the public Start page."),
    ("/setttings", "Open all bot settings."),
    ("/set", "Configure the Links Post caption, media and button."),
    ("/setstart", "Configure the Start page caption, media and buttons."),
    ("/broadcast", "Broadcast a message to all saved users."),
    ("/pbroadcast", "Broadcast and pin a message for all users."),
    ("/commands", "Browse this owner command guide."),
]


def all_settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps_label("Links Post"), callback_data="allsettings:links")],
        [InlineKeyboardButton(text=small_caps_label("Start Page"), callback_data="allsettings:start")],
        [InlineKeyboardButton(text=small_caps_label("Back"), callback_data="allsettings:back")],
    ])


def all_settings_body():
    return (
        "<b><i>⚙️ BOT SETTINGS</i></b>\n\n"
        "<i>Choose a settings section to manage the bot.</i>"
    )


@dp.message(Command("setttings"))
async def all_settings_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    await cancel_broadcast(message.from_user.id, message.chat.id)
    old_panel_ids = await clear_owner_panels(message.chat.id, message.from_user.id)
    sent = await message.answer(all_settings_body(), reply_markup=all_settings_keyboard())
    await ui_state.update_one(
        {"_id": f"settings:{message.chat.id}"},
        {"$set": {"message_id": sent.message_id, "settings_root": True}},
        upsert=True,
    )
    await finish_owner_command(message, old_panel_ids, sent.message_id)


@dp.callback_query(F.data.startswith("allsettings:"))
async def all_settings_navigation(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    await clear_owner_prompt(call.from_user.id)
    await call.answer()
    if action == "links":
        body = "<b><i>LINKS POST</i></b>\n\n<i>Edit caption, media, and button using the options below.</i>"
        await edit_panel(call.message, body, settings_keyboard("allsettings:home"))
    elif action == "start":
        await show_start_settings(call.message, back_callback="allsettings:home")
    elif action == "home":
        await edit_panel(call.message, all_settings_body(), all_settings_keyboard())
        await ui_state.update_one({"_id": f"settings:{call.message.chat.id}"}, {"$set": {"settings_root": True}}, upsert=True)
    elif action == "back":
        await render_home(call.message, edit_existing=True)


def commands_page(page: int = 0):
    total = max(1, (len(OWNER_COMMANDS) + COMMANDS_PAGE_SIZE - 1) // COMMANDS_PAGE_SIZE)
    page = max(0, min(page, total - 1))
    selected = OWNER_COMMANDS[page * COMMANDS_PAGE_SIZE:(page + 1) * COMMANDS_PAGE_SIZE]
    body = "<b><i>✦ ᴏᴡɴᴇʀ ᴄᴏᴍᴍᴀɴᴅs</i></b>\n\n"
    body += "\n".join(f"<b>{html.escape(command)}</b> — <i>{html.escape(description)}</i>" for command, description in selected)
    body += f"\n\n<i>Page {page + 1} of {total}</i>"
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=small_caps_label("Previous"), callback_data=f"commands:page:{page - 1}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text=small_caps_label("Next"), callback_data=f"commands:page:{page + 1}"))
    if nav:
        rows.append(nav)
    return body, InlineKeyboardMarkup(inline_keyboard=rows), page


@dp.message(Command("commands"))
async def commands_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    await cancel_broadcast(message.from_user.id, message.chat.id)
    old_panel_ids = await clear_owner_panels(message.chat.id, message.from_user.id)
    body, markup, page = commands_page(0)
    sent = await message.answer(body, reply_markup=markup)
    await ui_state.update_one(
        {"_id": f"commands:{message.chat.id}"},
        {"$set": {"message_id": sent.message_id, "page": page}},
        upsert=True,
    )
    await finish_owner_command(message, old_panel_ids, sent.message_id)


@dp.callback_query(F.data.startswith("commands:page:"))
async def commands_page_navigation(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    try:
        page = int(call.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Invalid page", show_alert=True)
        return
    await clear_owner_prompt(call.from_user.id)
    body, markup, page = commands_page(page)
    await call.answer()
    await edit_panel(call.message, body, markup)
    await ui_state.update_one({"_id": f"commands:{call.message.chat.id}"}, {"$set": {
        "message_id": call.message.message_id, "page": page,
    }}, upsert=True)


@dp.message(Command("set"))
async def set_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    await cancel_broadcast(message.from_user.id, message.chat.id)
    old_panel_ids = await clear_owner_panels(message.chat.id, message.from_user.id)
    sent = await message.answer(
        "<b><u>LINKS POST</u></b>\n\n"
        "<i>Edit caption, media, and button using the buttons below:</i>",
        reply_markup=settings_keyboard(),
    )
    await ui_state.update_one(
        {"_id": f"settings:{message.chat.id}"},
        {"$set": {"message_id": sent.message_id}},
        upsert=True,
    )
    await finish_owner_command(message, old_panel_ids, sent.message_id)


@dp.message(Command("setstart"))
async def setstart_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    await cancel_broadcast(message.from_user.id, message.chat.id)
    old_panel_ids = await clear_owner_panels(message.chat.id, message.from_user.id)
    await show_start_settings(message)
    fresh = await ui_state.find_one({"_id": f"settings:{message.chat.id}"})
    await finish_owner_command(
        message, old_panel_ids, fresh.get("message_id") if fresh else None
    )


@dp.callback_query(F.data.startswith("startcfg:"))
async def start_settings_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    config = await get_start_config()
    nav_state = await ui_state.find_one({"_id": f"settings:{call.message.chat.id}"})
    back_callback = "allsettings:home" if nav_state and nav_state.get("settings_root") else None
    if action != "cancel":
        await clear_owner_prompt(call.from_user.id)

    if action == "cancel":
        pending = await ui_state.find_one({"_id": f"prompt:{call.from_user.id}"})
        await clear_owner_prompt(call.from_user.id)
        await call.answer("Cancelled")
        if pending and str(pending.get("action", "")).startswith("start_button_"):
            body, markup = start_buttons_body(config), start_buttons_keyboard(config)
        elif pending and pending.get("action") == "start_caption":
            body, markup = "<b><u>CAPTION</u></b>\n\n<i>Customize your bot's start message Caption.</i>", start_submenu_keyboard("caption", config)
        elif pending and pending.get("action") == "start_media":
            body, markup = "<b><u>MEDIA</u></b>\n\n<i>Include an image or GIF with your start message.</i>", start_submenu_keyboard("media", config)
        else:
            body, markup = start_settings_body(config), start_settings_keyboard(config, back_callback)
        if pending:
            await BOT.edit_message_text(body, chat_id=pending["panel_chat_id"], message_id=pending["panel_message_id"], reply_markup=markup)
        return

    if action in {"home", "close"}:
        await clear_owner_prompt(call.from_user.id)
        await call.answer()
        await edit_panel(call.message, start_settings_body(config), start_settings_keyboard(config, back_callback))
        return

    if action == "about_toggle":
        enabled = not config.get("about_enabled", True)
        if enabled:
            await settings.update_one({"_id": START_CONFIG_ID}, {"$unset": {"about_enabled": ""}})
            await cleanup_empty_start_config()
        else:
            await settings.update_one({"_id": START_CONFIG_ID}, {"$set": {"about_enabled": False}}, upsert=True)
        config = await get_start_config()
        await call.answer("About enabled" if enabled else "About disabled")
        await edit_panel(call.message, start_settings_body(config), start_settings_keyboard(config, back_callback))
        return

    if action == "buttons":
        await clear_owner_prompt(call.from_user.id)
        await call.answer()
        await edit_panel(call.message, start_buttons_body(config), start_buttons_keyboard(config))
        return

    if action == "caption":
        await call.answer()
        body = "<b><u>CAPTION</u></b>\n\n<i>Customize your bot's start message Caption.</i>"
        await edit_panel(call.message, body, start_submenu_keyboard("caption", config))
        return
    if action == "caption_show":
        value = config.get("text") or render_start_text(config, await get_bot_info(), call.from_user)
        value = html.unescape(re.sub(r"<[^>]*>", "", value))
        value = re.sub(r"\s+", " ", value).strip()
        if len(value) > 200:
            value = value[:197] + "..."
        await call.answer(value, show_alert=True)
        return
    if action == "caption_default":
        await settings.update_one({"_id": START_CONFIG_ID}, {"$unset": {"text": ""}})
        await cleanup_empty_start_config()
        config = await get_start_config()
        await call.answer("✨ Successfully Restored to default.", show_alert=True)
        await edit_panel(call.message, "<b><u>CAPTION</u></b>\n\n<i>Customize your bot's start message Caption.</i>", start_submenu_keyboard("caption", config))
        return
    if action == "media":
        await call.answer()
        body = "<b><u>MEDIA</u></b>\n\n<i>Include an image or GIF with your start message.</i>"
        await edit_panel(call.message, body, start_submenu_keyboard("media", config))
        return
    if action == "media_remove":
        await settings.update_one({"_id": START_CONFIG_ID}, {"$unset": {"media_type": "", "media_file_id": ""}})
        await cleanup_empty_start_config()
        config = await get_start_config()
        await call.answer("✨ Successfully removed your bot start poster.", show_alert=True)
        await edit_panel(call.message, "<b><u>MEDIA</u></b>\n\n<i>Include an image or GIF with your start message.</i>", start_submenu_keyboard("media", config))
        return

    prompt_action = None
    prompt_text = None
    if action == "caption_edit":
        prompt_action, prompt_text = "start_caption", (
            "Send The New Caption text.\n\n"
            "Available variables (tap any item to copy):\n"
            "<code>{bot_name}</code> — Bot display name\n"
            "<code>{bot_mention}</code> — Linked bot name\n"
            "<code>{user_name}</code> — User display name\n"
            "<code>{mention}</code> — Linked user name\n"
            "<code>{anime}</code> — Lexi Anime link"
        )
    elif action == "media_edit":
        prompt_action, prompt_text = "start_media", "Only GIFs and images are supported. Send the new start media here."
    elif action.startswith("button") and action[6:].isdigit():
        index = int(action[6:])
        if index not in range(1, 5):
            await call.answer("Invalid button", show_alert=True)
            return
        label = (config.get(f"button{index}_text") or "").strip()
        url = (config.get(f"button{index}_url") or "").strip()
        if label and url:
            await settings.update_one({"_id": START_CONFIG_ID}, {"$unset": {
                f"button{index}_text": "", f"button{index}_url": "",
            }})
            config = await get_start_config()
            await call.answer(f"Button {index} disabled")
            await edit_panel(call.message, start_buttons_body(config), start_buttons_keyboard(config))
            return
        prompt_action = f"start_button_text_{index}"
        prompt_text = f"Send text for Start Message Button {index}."
    else:
        await call.answer("Unknown setting", show_alert=True)
        return

    prompt = await call.message.answer(f"<i>{prompt_text}</i>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps_label("Cancel"), callback_data="startcfg:cancel")]
    ]))
    await ui_state.update_one({"_id": f"prompt:{call.from_user.id}"}, {"$set": {
        "action": prompt_action, "panel_chat_id": call.message.chat.id,
        "panel_message_id": call.message.message_id, "prompt_message_id": prompt.message_id,
        "updated_at": datetime.now(timezone.utc),
    }}, upsert=True)
    await call.answer()


@dp.callback_query(F.data == "start:about")
async def show_start_about(call: CallbackQuery):
    # About is code-defined and always replaces the current Start message.
    body = render_about(await get_bot_info())
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❮❮ " + small_caps_label("Back"), callback_data="start:back")]
    ])
    try:
        if call.message.photo or call.message.animation or call.message.document:
            if telegram_html_length(body) > 1024:
                await call.answer(
                    "About text exceeds Telegram's 1024-character media-caption limit. "
                    "Shorten it in about.py to show it on this same media message.",
                    show_alert=True,
                )
                return
            await call.answer()
            await call.message.edit_caption(caption=body, reply_markup=markup)
        else:
            await call.answer()
            await call.message.edit_text(body, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramAPIError:
        log.exception("Could not edit the Start message to show About")


@dp.callback_query(F.data == "start:back")
async def back_from_start_about(call: CallbackQuery):
    # Restore the original Start caption/text and its configured URL buttons.
    config = await read_start_config()
    body = render_start_text(config, await get_bot_info(), call.from_user)
    markup = start_config_keyboard(config)
    try:
        if call.message.photo or call.message.animation or call.message.document:
            if telegram_html_length(body) > 1024:
                await call.answer(
                    "The configured Start caption exceeds Telegram's 1024-character media-caption limit.",
                    show_alert=True,
                )
                return
            await call.answer()
            await call.message.edit_caption(caption=body, reply_markup=markup)
        else:
            await call.answer()
            await call.message.edit_text(body, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramAPIError:
        log.exception("Could not restore the Start page after About")


@dp.callback_query(F.data.startswith("home:page:"))
async def home_page_navigation(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    try:
        page = max(0, int(call.data.rsplit(":", 1)[1]))
    except (ValueError, IndexError):
        await call.answer("Invalid page", show_alert=True)
        return
    await clear_owner_prompt(call.from_user.id)
    items = await chats.find({"active": True}).sort("created_at", 1).to_list(length=None)
    total_pages = max(1, (len(items) + HOME_PAGE_SIZE - 1) // HOME_PAGE_SIZE)
    page = min(page, total_pages - 1)
    await call.answer()
    await edit_panel(call.message, await home_text(), main_keyboard(items, page))
    await ui_state.update_one(
        {"_id": f"home:{call.message.chat.id}"},
        {"$set": {"message_id": call.message.message_id, "page": page}},
        upsert=True,
    )


@dp.callback_query(F.data == "home")
async def go_home(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    await clear_owner_prompt(call.from_user.id)
    await call.answer()
    await render_home(call.message, edit_existing=True)


@dp.callback_query(F.data == "add")
async def add_menu(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    try:
        await call.answer()
    except TelegramBadRequest as exc:
        # A delayed webhook can outlive Telegram's callback-query window.
        # Do not let an expired acknowledgement prevent the reply keyboard.
        if "query is too old" not in str(exc).lower() and "query id is invalid" not in str(exc).lower():
            raise
        log.warning("Add-chat callback expired; continuing to show picker keyboard.")

    await edit_panel(call.message, "<b>ADD CHAT</b>\n\n<i>Tap Channel or Group to open Telegram's chat picker directly.</i>", None)
    prompt = await call.message.answer(
        "<i>Select a chat:</i>",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(text=small_caps_label("Channel"), request_chat=KeyboardButtonRequestChat(
                    request_id=secrets.randbelow(2_000_000_000) + 1,
                    chat_is_channel=True,
                    request_title=True,
                    request_username=True,
                )),
                KeyboardButton(text=small_caps_label("Group"), request_chat=KeyboardButtonRequestChat(
                    request_id=secrets.randbelow(2_000_000_000) + 1,
                    chat_is_channel=False,
                    request_title=True,
                    request_username=True,
                )),
            ], [KeyboardButton(text=small_caps_label("Back"))]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    home_state = await ui_state.find_one({"_id": f"home:{call.message.chat.id}"}) or {}
    await ui_state.update_one(
        {"_id": f"picker:{call.from_user.id}"},
        {"$set": {"prompt_message_id": prompt.message_id,
                  "panel_chat_id": call.message.chat.id,
                  "panel_message_id": call.message.message_id,
                  "home_page": home_state.get("page", 0)}},
        upsert=True,
    )


@dp.callback_query(F.data.startswith("replace:"))
async def replace_chat_menu(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    code = call.data.split(":", 1)[1]
    item = await chats.find_one({"code": code, "active": True})
    if not item:
        await call.answer("Chat not found", show_alert=True)
        return
    await call.answer()
    await edit_panel(
        call.message,
        "<b>REPLACE CHAT</b>\n\n<i>Choose Channel or Group to select the new chat. The existing deep link will remain unchanged.</i>",
        None,
    )
    prompt = await call.message.answer(
        "<i>Select the replacement chat:</i>",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(text=small_caps_label("Channel"), request_chat=KeyboardButtonRequestChat(
                    request_id=secrets.randbelow(2_000_000_000) + 1,
                    chat_is_channel=True, request_title=True, request_username=True,
                )),
                KeyboardButton(text=small_caps_label("Group"), request_chat=KeyboardButtonRequestChat(
                    request_id=secrets.randbelow(2_000_000_000) + 1,
                    chat_is_channel=False, request_title=True, request_username=True,
                )),
            ], [KeyboardButton(text=small_caps_label("Back"))]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    home_state = await ui_state.find_one({"_id": f"home:{call.message.chat.id}"}) or {}
    await ui_state.update_one(
        {"_id": f"picker:{call.from_user.id}"},
        {"$set": {
            "action": "replace",
            "target_code": code,
            "prompt_message_id": prompt.message_id,
            "panel_chat_id": call.message.chat.id,
            "panel_message_id": call.message.message_id,
            "home_page": home_state.get("page", 0),
        }},
        upsert=True,
    )


async def restore_picker_panel(state, notice=None):
    try:
        await BOT.delete_message(state["panel_chat_id"], state["prompt_message_id"])
    except (TelegramAPIError, KeyError):
        pass
    try:
        await BOT.edit_message_text(
            await home_text(),
            chat_id=state["panel_chat_id"],
            message_id=state["panel_message_id"],
            reply_markup=main_keyboard(
                await chats.find({"active": True}).sort("created_at", 1).to_list(length=None),
                state.get("home_page", 0),
            ),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.warning("Could not restore home panel after chat selection: %s", exc)
    if notice:
        await BOT.send_message(state["panel_chat_id"], notice, reply_markup=ReplyKeyboardRemove())
    else:
        await BOT.send_message(state["panel_chat_id"], "Selection closed.", reply_markup=ReplyKeyboardRemove())


@dp.message(F.text == "Back")
async def picker_back(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    state = await ui_state.find_one({"_id": f"picker:{message.from_user.id}"})
    if not state:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    await ui_state.delete_one({"_id": f"picker:{message.from_user.id}"})
    await restore_picker_panel(state, "Chat selection cancelled.")

@dp.message(F.chat_shared)
async def chat_shared(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    state = await ui_state.find_one({"_id": f"picker:{message.from_user.id}"})
    if not state:
        return
    # Telegram sends the selected chat as a service message in the owner's chat.
    # Remove it immediately so "You shared a chat with the bot" never remains visible.
    try:
        await message.delete()
    except TelegramAPIError as delete_error:
        log.warning("Could not delete chat-shared service message %s: %s", message.message_id, delete_error)
    try:
        shared = message.chat_shared
        chat, member = await asyncio.gather(
            BOT.get_chat(shared.chat_id),
            BOT.get_chat_member(shared.chat_id, current_bot_user_id.get()),
        )
        if member.status not in {"administrator", "creator"}:
            await ui_state.delete_one({"_id": f"picker:{message.from_user.id}"})
            await restore_picker_panel(state, "Please make the bot an administrator in the selected chat and grant it permission to invite users.")
            return
        if not getattr(member, "can_invite_users", False) and member.status != "creator":
            await ui_state.delete_one({"_id": f"picker:{message.from_user.id}"})
            await restore_picker_panel(state, "Please grant the bot permission to invite users, then select the chat again.")
            return
        action = state.get("action", "add")
        title = chat.title or getattr(shared, "title", None) or str(shared.chat_id)
        username = getattr(chat, "username", None)
        if action == "replace":
            target = await chats.find_one({"code": state.get("target_code"), "active": True})
            if not target:
                raise RuntimeError("The chat selected for replacement could not be found.")
            existing = await chats.find_one({"chat_id": shared.chat_id, "active": True})
            if existing and existing["_id"] != target["_id"]:
                await ui_state.delete_one({"_id": f"picker:{message.from_user.id}"})
                await restore_picker_panel(
                    state, f"This chat has already been added: {html.escape(existing.get('title', str(shared.chat_id)))}"
                )
                return
            # Revoke any outstanding invite links for the old destination before switching.
            failed_records = []
            async for record in issued.find({"chat_id": target["chat_id"], "revoked": False}):
                if not await revoke_record(record):
                    failed_records.append(record)
                else:
                    task = expiry_tasks.get(str(record["_id"]))
                    if task and task is not asyncio.current_task():
                        task.cancel()
                    await issued.delete_one({"_id": record["_id"]})
            if failed_records:
                details = "; ".join(
                    f"{r.get('invite_link')}: {r.get('_last_revoke_error', 'revocation failed')}"
                    for r in failed_records
                )
                raise RuntimeError("Could not revoke every active invite for the old chat. No replacement was made. " + details[:500])
            await chats.update_one(
                {"_id": target["_id"]},
                {"$set": {
                    "chat_id": shared.chat_id,
                    "title": title,
                    "username": username,
                    "is_private": not bool(username),
                    "active": True,
                    "replaced_at": datetime.now(timezone.utc),
                }},
            )
            notice = "<i>✨ Chat replaced successfully. The existing deep link remains unchanged and now points to the new chat.</i>"
        else:
            existing = await chats.find_one({"chat_id": shared.chat_id, "active": True})
            if existing:
                await ui_state.delete_one({"_id": f"picker:{message.from_user.id}"})
                await restore_picker_panel(
                    state, f"This chat has already been added: {html.escape(existing.get('title', str(shared.chat_id)))}"
                )
                return
            code = "".join(secrets.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(25))
            await chats.insert_one({
                "chat_id": shared.chat_id,
                "title": title,
                "code": code,
                "username": username,
                "is_private": not bool(username),
                "active": True,
                "created_at": datetime.now(timezone.utc),
            })
            notice = "<i>✨ Chat added successfully.</i>"
        await ui_state.delete_one({"_id": f"picker:{message.from_user.id}"})
        await restore_picker_panel(state, notice)
    except Exception as exc:
        log.exception("Unable to save selected chat")
        await ui_state.delete_one({"_id": f"picker:{message.from_user.id}"})
        error_detail = str(exc).strip()
        notice = f"Could not add the chat: {html.escape(error_detail[:300] or type(exc).__name__)}"
        await restore_picker_panel(state, notice)


@dp.callback_query(F.data.startswith("chat:"))
async def chat_detail(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    code = call.data.split(":", 1)[1]
    item = await chats.find_one({"code": code, "active": True})
    if not item:
        await call.answer("Chat not found", show_alert=True)
        return
    deep_link = f"https://t.me/{current_bot_username.get()}?start={code}"
    body = (
        f"<i>Chat Name:</i> {html.escape(item['title'])}\n\n"
        f"<i>Deeplink:</i> <code>{html.escape(deep_link)}</code>"
    )
    await call.answer()
    await edit_panel(call.message, body, detail_keyboard(code))


@dp.callback_query(F.data == "settings")
async def settings_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    await clear_owner_prompt(call.from_user.id)
    state = await ui_state.find_one({"_id": f"settings:{call.message.chat.id}"})
    await call.answer()
    await show_settings(
        call.message,
        back_callback="allsettings:home" if state and state.get("settings_root") else "home",
    )


@dp.callback_query(F.data == "cfg:default")
async def cfg_default(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    await settings.delete_one({"_id": "post"})
    invalidate_config_cache()
    await call.answer("✨ ✅ Successfully reset all to defaults")
    await show_settings(call.message)


@dp.callback_query(F.data.in_({"cfg:media", "cfg:caption", "cfg:button"}))
async def cfg_prompt(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    await clear_owner_prompt(call.from_user.id)
    prompts = {
        "media": "<i>Send new media for the links post. Only GIF and picture are supported.</i>",
        "caption": "<i>Send new caption for the Link Post.</i>",
        "button": "<i>Send new button text for the Link Post Button.</i>",
    }
    await call.answer()
    prompt = await call.message.answer(
        prompts[action],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=small_caps_label("Cancel"), callback_data="settings")]
        ]),
    )
    await ui_state.update_one(
        {"_id": f"prompt:{call.from_user.id}"},
        {"$set": {
            "action": action,
            "panel_chat_id": call.message.chat.id,
            "panel_message_id": call.message.message_id,
            "prompt_message_id": prompt.message_id,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


@dp.message(F.text.startswith("/"))
async def unknown_owner_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    await cancel_broadcast(message.from_user.id, message.chat.id)
    old_panel_ids = await clear_owner_panels(message.chat.id, message.from_user.id)
    await render_home(message)
    fresh = await ui_state.find_one({"_id": f"home:{message.chat.id}"})
    await finish_owner_command(message, old_panel_ids, fresh.get("message_id") if fresh else None)


@dp.message()
async def owner_input(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    broadcast_state = await ui_state.find_one({"_id": f"broadcast:{message.from_user.id}"})
    if broadcast_state:
        task = broadcast_tasks.pop(message.from_user.id, None)
        if task and not task.done():
            task.cancel()
        await ui_state.delete_one({"_id": f"broadcast:{message.from_user.id}"})
        try:
            await BOT.delete_message(broadcast_state["chat_id"], broadcast_state["prompt_message_id"])
        except (TelegramAPIError, KeyError):
            pass
        await run_broadcast(message, broadcast_state["chat_id"], bool(broadcast_state.get("pin")))
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return
    pending = await ui_state.find_one({"_id": f"prompt:{message.from_user.id}"})
    if not pending:
        return
    action = pending["action"]

    async def reject_input(notice: str):
        # Never leave rejected owner input visible as a stray chat message.
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        prompt = notice
        if action.startswith("start_button_text_"):
            prompt += "\n\nSend button text (1–64 characters)."
        elif action.startswith("start_button_url_"):
            prompt += "\n\nSend a complete http:// or https:// URL."
        elif action == "start_caption":
            prompt += "\n\nSend the new /start caption."
        elif action == "start_media":
            prompt += "\n\nSend one photo or GIF."
        elif action == "caption":
            prompt += "\n\nSend the new Link Post caption."
        elif action == "button":
            prompt += "\n\nSend button text (1–64 characters)."
        elif action == "media":
            prompt += "\n\nSend a photo or GIF."
        if pending.get("prompt_message_id"):
            try:
                await BOT.delete_message(pending["panel_chat_id"], pending["prompt_message_id"])
            except TelegramAPIError:
                pass
        retry_prompt = await BOT.send_message(
            pending["panel_chat_id"], f"<i>{html.escape(prompt)}</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=small_caps_label("Cancel"), callback_data=(
                    "startcfg:cancel" if action.startswith("start_") else "settings"
                ))
            ]]),
        )
        await ui_state.update_one({"_id": f"prompt:{message.from_user.id}"}, {"$set": {
            "prompt_message_id": retry_prompt.message_id,
        }})

    if action.startswith("start_button_text_"):
        text_value = (message.text or "").strip()
        if not text_value or len(text_value) > 64:
            await reject_input("Invalid button text.")
            return
        index = action.rsplit("_", 1)[1]
        await ui_state.update_one({"_id": f"prompt:{message.from_user.id}"}, {"$set": {
            "button_text": text_value, "action": f"start_button_url_{index}",
        }})
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        if pending.get("prompt_message_id"):
            try:
                await BOT.delete_message(pending["panel_chat_id"], pending["prompt_message_id"])
            except TelegramAPIError:
                pass
        next_prompt = await BOT.send_message(
            pending["panel_chat_id"], f"<i>Now send the URL for Start Message Button {index}.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=small_caps_label("Cancel"), callback_data="startcfg:cancel")
            ]]),
        )
        await ui_state.update_one({"_id": f"prompt:{message.from_user.id}"}, {"$set": {
            "prompt_message_id": next_prompt.message_id,
        }})
        return

    if action.startswith("start_button_url_"):
        index = action.rsplit("_", 1)[1]
        url = (message.text or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            await reject_input("Invalid URL.")
            return
        label = (pending or {}).get("button_text", "")
        if not label:
            await reject_input("Button text is missing. Cancel and start again.")
            return
        await settings.update_one({"_id": START_CONFIG_ID}, {"$set": {
            f"button{index}_text": label, f"button{index}_url": url,
        }}, upsert=True)

    elif action == "start_caption":
        value = message.text or message.caption
        if value is None:
            await reject_input("Please send text for the Start caption.")
            return
        start_cfg = await get_start_config()
        limit = 1024 if start_cfg.get("media_file_id") else 4096
        if telegram_html_length(value) > limit:
            await reject_input(f"Caption is too long. Telegram allows {limit} characters here.")
            return
        await settings.update_one({"_id": START_CONFIG_ID}, {"$set": {"text": value}}, upsert=True)

    elif action == "start_media":
        if message.photo:
            media_type, file_id = "photo", message.photo[-1].file_id
        elif message.animation:
            media_type, file_id = "animation", message.animation.file_id
        else:
            await reject_input("Please send a photo or GIF.")
            return
        start_cfg = await get_start_config()
        if telegram_html_length(start_cfg.get("text") or render_start_text(start_cfg, await get_bot_info(), message.from_user)) > 1024:
            await reject_input("Media not changed: shorten the Start caption to 1024 characters first.")
            return
        await settings.update_one({"_id": START_CONFIG_ID}, {"$set": {
            "media_type": media_type, "media_file_id": file_id,
        }}, upsert=True)

    elif action == "caption":
        text_value = message.text or message.caption
        if not text_value:
            await reject_input("Please send text for the caption.")
            return
        cfg_now = await get_config()
        caption_limit = 1024 if cfg_now.get("media_file_id") else 4096
        if telegram_text_length(text_value) > caption_limit:
            await reject_input(f"Caption is too long. Telegram allows up to {caption_limit} characters.")
            return
        entities = message.entities if message.text else message.caption_entities
        await settings.update_one({"_id": "post"}, {"$set": {
            "caption": text_value,
            "caption_entities": [e.model_dump(mode="json", exclude_none=True) for e in (entities or [])],
        }}, upsert=True)
        invalidate_config_cache()

    elif action == "button":
        if not message.text or len(message.text) > 64:
            await reject_input("Please send button text between 1 and 64 characters.")
            return
        await settings.update_one({"_id": "post"}, {"$set": {"button": message.text}}, upsert=True)
        invalidate_config_cache()

    elif action == "media":
        if message.photo:
            media_type, file_id = "photo", message.photo[-1].file_id
        elif message.animation:
            media_type, file_id = "animation", message.animation.file_id
        else:
            await reject_input("Please send only a picture or GIF.")
            return
        current_config = await get_config()
        if telegram_text_length(current_config.get("caption", DEFAULT_CAPTION)) > 1024:
            await reject_input("Media not changed: shorten the post caption to 1024 characters first.")
            return
        await settings.update_one({"_id": "post"}, {"$set": {
            "media_type": media_type, "media_file_id": file_id,
        }}, upsert=True)
        invalidate_config_cache()
    else:
        await ui_state.delete_one({"_id": f"prompt:{message.from_user.id}"})
        return

    await clear_owner_prompt(message.from_user.id)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    try:
        if action.startswith("start_"):
            config = await get_start_config()
            if action == "start_caption":
                success = "<i>✨ Successfully Updated New Start Message Caption.</i>"
                markup = start_submenu_keyboard("caption", config)
            elif action == "start_media":
                success = "<i>✨ Start Message media added successfully.</i>"
                markup = start_submenu_keyboard("media", config)
            elif action.startswith("start_button_url_"):
                index = action.rsplit("_", 1)[1]
                success = f"<i>✨ Start Message Button {index} added successfully.</i>\n\n" + start_buttons_body(config)
                markup = start_buttons_keyboard(config)
            else:
                success = start_settings_body(config)
                markup = start_settings_keyboard(config)
            await BOT.edit_message_text(
                success,
                chat_id=pending["panel_chat_id"], message_id=pending["panel_message_id"],
                reply_markup=markup,
            )
        else:
            await BOT.edit_message_text(
                "<b><u>LINKS POST</u></b>\n\n<i>Edit caption, media, and button using the buttons below:</i>",
                chat_id=pending["panel_chat_id"], message_id=pending["panel_message_id"],
                reply_markup=settings_keyboard(),
            )
    except TelegramAPIError:
        log.exception("Could not restore settings panel")



async def update_chat_title_from_service(message: Message):
    if not message.new_chat_title:
        return
    result = await chats.update_one(
        {"chat_id": message.chat.id, "active": True},
        {"$set": {"title": message.new_chat_title}},
    )
    if result.modified_count:
        await refresh_home_panels()


@dp.message(F.new_chat_title)
async def group_title_changed(message: Message):
    await update_chat_title_from_service(message)


@dp.channel_post(F.new_chat_title)
async def channel_title_changed(message: Message):
    await update_chat_title_from_service(message)


@dp.chat_member()
async def member_update(update: ChatMemberUpdated):
    if update.new_chat_member.status not in {"member", "administrator", "creator"}:
        return
    invite_url = update.invite_link.invite_link if update.invite_link else None
    record = None
    if invite_url:
        record = await issued.find_one({"invite_link": invite_url, "revoked": False})
    if not record:
        record = await issued.find_one(
            {
                "chat_id": update.chat.id,
                "user_id": update.new_chat_member.user.id,
                "revoked": False,
            },
            sort=[("created_at", -1)],
        )
    if record and await revoke_record(record):
        task = expiry_tasks.get(str(record["_id"]))
        if task and task is not asyncio.current_task():
            task.cancel()
        await issued.delete_one({"_id": record["_id"]})


async def revoke_record(record):
    try:
        await BOT.revoke_chat_invite_link(record["chat_id"], record["invite_link"])
    except TelegramBadRequest as exc:
        error_text = str(exc).lower()
        already_inactive = any(phrase in error_text for phrase in (
            "invite link is already revoked",
            "invite link has already been revoked",
            "invite link has expired",
            "invite link is expired",
        ))
        if not already_inactive:
            record["_last_revoke_error"] = f"{type(exc).__name__}: {exc}"
            log.error("Telegram rejected invite revoke for %s: %s", record.get("chat_id"), exc)
            return False
        log.info("Invite is already inactive: %s", exc)
    except TelegramAPIError as exc:
        record["_last_revoke_error"] = f"{type(exc).__name__}: {exc}"
        log.error("Invite revoke API failure for chat %s: %s", record.get("chat_id"), exc)
        return False
    await issued.update_one(
        {"_id": record["_id"]},
        {"$set": {"revoked": True, "revoked_at": datetime.now(timezone.utc)}},
    )
    return True


def schedule_expiry(record):
    key = str(record["_id"])
    old_task = expiry_tasks.get(key)
    if old_task and not old_task.done():
        old_task.cancel()
    expiry_tasks[key] = asyncio.create_task(expire_one_link(record))


async def expire_one_link(record):
    key = str(record["_id"])
    # Restored expiry tasks must revoke through the same bot that created the invite.
    info = bot_registry.get(record.get("bot_id")) or bot_registry.get(default_bot.id)
    if info:
        db_token = current_db.set(bot_databases[info["bot"].id])
        bot_token = current_bot.set(info["bot"])
        username_token = current_bot_username.set(info.get("username", ""))
        user_id_token = current_bot_user_id.set(info["bot"].id)
    else:
        db_token = bot_token = username_token = user_id_token = None
    try:
        expires_at = record["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        delay = max(0, (expires_at - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(delay)
        last_error = "Unknown revoke failure"
        for attempt in range(4):
            try:
                latest = await issued.find_one({"_id": record["_id"]})
                if not latest:
                    return
                if latest.get("revoked") or await revoke_record(latest):
                    await issued.delete_one({"_id": record["_id"]})
                    return
                last_error = latest.get("_last_revoke_error", "Telegram did not confirm revocation; no API error text was returned.")
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Expiry attempt %s failed for invite %s", attempt + 1, record.get("invite_link"))
            if attempt < 3:
                await asyncio.sleep(30)
        details = (
            "⚠️ <b>INVITE REVOKE FAILED</b>\n\n"
            f"<b>Chat:</b> {html.escape(record.get('chat_title', str(record['chat_id'])))}\n"
            f"<b>Chat ID:</b> <code>{record['chat_id']}</code>\n"
            f"<b>Invite:</b> <code>{html.escape(record['invite_link'])}</code>\n"
            f"<b>Error:</b> <code>{html.escape(last_error)}</code>\n"
            "<b>Attempts:</b> 1 initial + 3 retries"
        )
        for owner_id in OWNER_IDS:
            try:
                await BOT.send_message(owner_id, details)
            except TelegramAPIError:
                log.exception("Could not notify owner %s about failed invite revoke", owner_id)
        # After all 4 revoke attempts fail, remove this invite's database record
        # after attempting owner notifications, even if a notification could not be delivered.
        await issued.delete_one({"_id": record["_id"]})
    except asyncio.CancelledError:
        raise
    finally:
        if expiry_tasks.get(key) is asyncio.current_task():
            expiry_tasks.pop(key, None)
        if db_token is not None:
            current_db.reset(db_token)
        if bot_token is not None:
            current_bot.reset(bot_token)
            current_bot_username.reset(username_token)
            current_bot_user_id.reset(user_id_token)


async def active_bot_middleware(handler, event, data):
    bot = data.get("bot")
    if bot is None:
        return await handler(event, data)
    me = bot_registry.get(bot.id, {})
    db_token = current_db.set(bot_databases[bot.id])
    bot_token = current_bot.set(bot)
    username_token = current_bot_username.set(me.get("username", ""))
    user_id_token = current_bot_user_id.set(bot.id)
    try:
        event_message = event if isinstance(event, Message) else getattr(event, "message", None)
        user = getattr(event, "from_user", None)
        if user is not None and getattr(getattr(event_message, "chat", None), "type", None) == "private":
            try:
                await track_user_id(user.id)
            except Exception:
                log.exception("Could not add Telegram user %s to users collection", user.id)
        return await handler(event, data)
    finally:
        current_db.reset(db_token)
        current_bot.reset(bot_token)
        current_bot_username.reset(username_token)
        current_bot_user_id.reset(user_id_token)


async def telegram_webhook(request):
    bot_id = int(request.match_info["bot_id"])
    bot_info = request.app["bots"].get(bot_id)
    if bot_info is None:
        raise web.HTTPNotFound()
    # bot_registry stores metadata; aiogram requires the actual Bot instance.
    bot = bot_info["bot"]
    secret = request.app.get("webhook_secret")
    if not secret or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        raise web.HTTPUnauthorized()
    try:
        from aiogram.types import Update
        update = Update.model_validate(await request.json(), context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("Failed to process Telegram webhook update")
        raise web.HTTPInternalServerError()
    return web.Response(text="ok")


async def health(request):
    try:
        await mongo.admin.command("ping")
        return web.json_response({"status": "ok", "service": "Link-Share", "database": "connected"})
    except Exception:
        log.exception("Health check database ping failed")
        return web.json_response({"status": "degraded", "service": "Link-Share"}, status=503)


async def main():
    global default_bot, default_db, bot_info_cache
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    runner = None
    try:
        me = await bot.get_me()
        bot_info_cache = me
        # Request only the admin capability required by Link Share: creating invite links.
        invite_admin_rights = ChatAdministratorRights(
            is_anonymous=False,
            can_manage_chat=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=True,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
            can_send_welcome_messages=False,
        )
        await bot.set_my_default_administrator_rights(
            rights=invite_admin_rights,
            for_channels=False,
        )
        await bot.set_my_default_administrator_rights(
            rights=invite_admin_rights,
            for_channels=True,
        )
        # Public users see only /start. Each configured owner gets the full menu
        # in their private chat; authorization is still enforced by every handler.
        await bot.set_my_commands([
            BotCommand(command="start", description="Start the Adventure ✨"),
        ])
        # Owner menu exposes the settings hub and supported owner operations.
        owner_commands = [
            BotCommand(command="start", description="Start the Adventure ✨"),
            BotCommand(command="setttings", description="Open all bot settings"),
            BotCommand(command="set", description="Configure Links Post"),
            BotCommand(command="setstart", description="Configure Start Post & About"),
            BotCommand(command="broadcast", description="Broadcast a message to all users"),
            BotCommand(command="pbroadcast", description="Broadcast and pin a message for all users"),
            BotCommand(command="commands", description="Show Owner Commands"),
        ]
        for owner_id in OWNER_IDS:
            await bot.set_my_commands(
                owner_commands,
                scope=BotCommandScopeChat(chat_id=owner_id),
            )
        log.info(
            "Bot command menus registered: public=/start; owner chats=%s",
            len(OWNER_IDS),
        )
        default_bot = bot
        bot_registry[me.id] = {"bot": bot, "username": me.username or ""}
        dp.update.outer_middleware(active_bot_middleware)

        await mongo.admin.command("ping")
        # Single-bot mode uses the original database so existing chats and deep links stay valid.
        bot_databases[bot.id] = mongo[DB_NAME]
        default_db = bot_databases[bot.id]
        # Keep the users collection to exactly one document with only the id data field.
        await users.delete_many({"_id": {"$ne": "all"}})
        await users.update_one(
            {"_id": "all"},
            {"$setOnInsert": {"id": ""}},
            upsert=True,
        )

        db_token = current_db.set(default_db)
        bot_token = current_bot.set(bot)
        username_token = current_bot_username.set(me.username or "")
        user_id_token = current_bot_user_id.set(bot.id)
        try:
            await get_config()
            await chats.create_index("code", unique=True, sparse=True, name="uniq_chat_deepcode")
            await chats.create_index([("chat_id", 1)], unique=True, name="uniq_chat_id")
            await issued.create_index([("expires_at", 1), ("revoked", 1)], name="invite_expiry")
            await issued.create_index([("chat_id", 1), ("user_id", 1), ("created_at", -1)], name="invite_user_lookup")
            async for record in issued.find({"revoked": False}):
                schedule_expiry(record)
        finally:
            current_bot_user_id.reset(user_id_token)
            current_bot_username.reset(username_token)
            current_bot.reset(bot_token)
            current_db.reset(db_token)

        public_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)
        app = web.Application()
        app["webhook_secret"] = webhook_secret
        app["bots"] = bot_registry
        app.router.add_get("/", health)
        app.router.add_get("/health", health)

        if public_url:
            app.router.add_post("/telegram/webhook/{bot_id}", telegram_webhook)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000")))
            await site.start()
            await bot.set_webhook(
                url=f"{public_url}/telegram/webhook/{bot.id}",
                secret_token=webhook_secret,
                allowed_updates=["message", "channel_post", "callback_query", "chat_member", "chat_join_request"],
                drop_pending_updates=False,
            )
            log.info("Webhook active for @%s at %s/telegram/webhook/%s", me.username, public_url, bot.id)
            await asyncio.Event().wait()
        else:
            log.warning("RENDER_EXTERNAL_URL missing; using polling fallback.")
            await dp.start_polling(
                bot,
                allowed_updates=["message", "channel_post", "callback_query", "chat_member", "chat_join_request"],
            )
    finally:
        for task in list(expiry_tasks.values()):
            task.cancel()
        if expiry_tasks:
            await asyncio.gather(*list(expiry_tasks.values()), return_exceptions=True)
        if runner:
            await runner.cleanup()
        await bot.session.close()
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())