import asyncio
import html
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, KeyboardButtonRequestChat, Message, ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("link-share")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
OWNER_IDS = {int(value.strip()) for value in os.environ["OWNER"].split(",") if value.strip()}
if not OWNER_IDS:
    raise RuntimeError("OWNER must contain at least one Telegram user ID.")
MONGO_URI = os.environ.get("URI", "").strip()
DB_NAME = os.environ.get("DB_NAME", "link_share").strip() or "link_share"
if not BOT_TOKEN or not MONGO_URI:
    raise RuntimeError("BOT_TOKEN and URI must be set in Render environment variables.")

mongo = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=20000, connectTimeoutMS=20000)
db = mongo[DB_NAME]
chats = db.chats
settings = db.settings
issued = db.issued_links
ui_state = db.owner_ui

DEFAULT_CAPTION = "<b><i>» Hᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ!</i></b>\\n<b>» ᴄʟɪᴄᴋ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴘʀᴏᴄᴇᴇᴅ:</b>"
DEFAULT_BUTTON = "• ᴄʟɪᴄᴋ ʜᴇʀᴇ •"
BOT: Bot
dp = Dispatcher()
bot_username = ""
bot_user_id = 0
expiry_tasks = {}
POST_CONFIG_CACHE = None


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def small_caps(value: str) -> str:
    return value.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    ))


def main_keyboard(items):
    rows, row = [], []
    for item in items:
        row.append(InlineKeyboardButton(
            text=item["title"][:64],
            callback_data=f"chat:{item['code']}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="+", callback_data="add")])
    rows.append([InlineKeyboardButton(text="ʟɪɴᴋs ᴘᴏsᴛ", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Channel", callback_data="pick:channel"),
         InlineKeyboardButton(text="Group", callback_data="pick:group")],
        [InlineKeyboardButton(text="Back", callback_data="home")],
    ])


def settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ᴅᴇꜰᴀᴜʟᴛ", callback_data="cfg:default"),
         InlineKeyboardButton(text="ᴍᴇᴅɪᴀ", callback_data="cfg:media")],
        [InlineKeyboardButton(text="ᴄᴀᴘᴛɪᴏɴ", callback_data="cfg:caption"),
         InlineKeyboardButton(text="ʙᴜᴛᴛᴏɴ", callback_data="cfg:button")],
        [InlineKeyboardButton(text="Back", callback_data="home")],
    ])


def detail_keyboard(code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Replace Chat", callback_data=f"replace:{code}")],
        [InlineKeyboardButton(text="Back", callback_data="home")],
    ])


async def get_config():
    global POST_CONFIG_CACHE
    if POST_CONFIG_CACHE is not None:
        return POST_CONFIG_CACHE
    defaults = {
        "_id": "post", "caption": DEFAULT_CAPTION, "caption_entities": [],
        "button": DEFAULT_BUTTON, "media_type": None, "media_file_id": None,
    }
    config = await settings.find_one({"_id": "post"})
    old_default_caption = "<blockquote><b><i>» Hᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ!</i></b></blockquote><b>» ᴄʟɪᴄᴋ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴘʀᴏᴄᴇᴇᴅ:</b>"
    if config and config.get("caption") == old_default_caption:
        await settings.update_one(
            {"_id": "post"},
            {"$set": {"caption": DEFAULT_CAPTION, "caption_entities": []}},
        )
        config["caption"] = DEFAULT_CAPTION
        config["caption_entities"] = []
    if config is None:
        await settings.update_one({"_id": "post"}, {"$setOnInsert": defaults}, upsert=True)
        config = await settings.find_one({"_id": "post"})
    POST_CONFIG_CACHE = config or defaults
    return POST_CONFIG_CACHE


def invalidate_config_cache():
    global POST_CONFIG_CACHE
    POST_CONFIG_CACHE = None


async def home_text():
    return ("<b><u>ROTATION LINKS CHATS</u></b>\n\n"
            "<i>Select any chat for links delivery. A new link will be used every time to avoid spammers.</i>")


async def clear_owner_panels(chat_id: int, owner_id: int):
    # Read and clear independent UI state concurrently to reduce command latency.
    keys = [f"home:{chat_id}", f"settings:{chat_id}", f"picker:{owner_id}", f"prompt:{owner_id}"]
    saved = await asyncio.gather(*(ui_state.find_one({"_id": key}) for key in keys))
    old_panel_ids = [
        item["message_id"] for item in saved[:2]
        if item and item.get("message_id")
    ]
    await asyncio.gather(*(ui_state.delete_one({"_id": key}) for key in keys))
    picker = saved[2]
    if picker and picker.get("prompt_message_id"):
        try:
            await BOT.delete_message(chat_id, picker["prompt_message_id"])
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
    markup = main_keyboard(items)
    async for panel in ui_state.find({"_id": {"$regex": "^home:"}}):
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
    body, markup = await home_text(), main_keyboard(items)
    panel = await ui_state.find_one({"_id": f"home:{message.chat.id}"})
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


async def show_settings(message: Message):
    body = (
        "<b><u>LINKS POST</u></b>\n\n"
        "<i>Edit caption, media, and button using the buttons below:</i>"
    )
    await edit_panel(message, body, settings_keyboard())
    await ui_state.update_one({"_id": f"settings:{message.chat.id}"},
        {"$set": {"message_id": message.message_id}}, upsert=True)


def request_chat_keyboard(kind: str):
    request_id = secrets.randbelow(2_000_000_000) + 1
    button = KeyboardButton(
        text=f"Choose {kind.title()}",
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
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
            "revoked": False,
        }
        result = await issued.insert_one(record)
        record["_id"] = result.inserted_id
        schedule_expiry(record)
        cfg = await get_config()
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
            "reply_parameters": ReplyParameters(message_id=message.message_id),
        }
        if entities:
            kwargs["caption_entities"] = entities
            kwargs["parse_mode"] = None
        else:
            kwargs["parse_mode"] = ParseMode.HTML
        if media_id and media_type == "photo":
            await message.answer_photo(media_id, **kwargs)
        elif media_id and media_type == "animation":
            await message.answer_animation(media_id, **kwargs)
        else:
            await message.answer(
                caption,
                entities=entities,
                parse_mode=None if entities else ParseMode.HTML,
                reply_markup=markup,
                reply_parameters=ReplyParameters(message_id=message.message_id),
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
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    doc = await chats.find_one({"code": code, "active": True}) if code else None
    if not doc:
        await message.answer("This link is invalid or no longer available.")
        return
    await send_fresh_link(message, doc)


@dp.message(Command("admin"))
@dp.message(Command("owner"))
async def owner_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    old_panel_ids = await clear_owner_panels(message.chat.id, message.from_user.id)
    await render_home(message)
    fresh = await ui_state.find_one({"_id": f"home:{message.chat.id}"})
    await finish_owner_command(message, old_panel_ids, fresh.get("message_id") if fresh else None)


@dp.message(Command("set"))
async def set_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass
@dp.callback_query(F.data == "home")
async def go_home(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
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
        "Select a chat:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(text="Channel", request_chat=KeyboardButtonRequestChat(
                    request_id=secrets.randbelow(2_000_000_000) + 1,
                    chat_is_channel=True,
                    request_title=True,
                    request_username=True,
                )),
                KeyboardButton(text="Group", request_chat=KeyboardButtonRequestChat(
                    request_id=secrets.randbelow(2_000_000_000) + 1,
                    chat_is_channel=False,
                    request_title=True,
                    request_username=True,
                )),
            ], [KeyboardButton(text="Back")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    await ui_state.update_one(
        {"_id": f"picker:{call.from_user.id}"},
        {"$set": {"prompt_message_id": prompt.message_id,
                  "panel_chat_id": call.message.chat.id,
                  "panel_message_id": call.message.message_id}},
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
        "Select the replacement chat:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(text="Channel", request_chat=KeyboardButtonRequestChat(
                    request_id=secrets.randbelow(2_000_000_000) + 1,
                    chat_is_channel=True, request_title=True, request_username=True,
                )),
                KeyboardButton(text="Group", request_chat=KeyboardButtonRequestChat(
                    request_id=secrets.randbelow(2_000_000_000) + 1,
                    chat_is_channel=False, request_title=True, request_username=True,
                )),
            ], [KeyboardButton(text="Back")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    await ui_state.update_one(
        {"_id": f"picker:{call.from_user.id}"},
        {"$set": {
            "action": "replace",
            "target_code": code,
            "prompt_message_id": prompt.message_id,
            "panel_chat_id": call.message.chat.id,
            "panel_message_id": call.message.message_id,
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
            reply_markup=main_keyboard(await chats.find({"active": True}).sort("created_at", 1).to_list(length=None)),
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
            BOT.get_chat_member(shared.chat_id, bot_user_id),
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
            notice = "Chat replaced successfully. The existing deep link remains unchanged and now points to the new chat."
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
            notice = "Chat added successfully."
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
    deep_link = f"https://t.me/{bot_username}?start={code}"
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
    await ui_state.delete_one({"_id": f"prompt:{call.from_user.id}"})
    await call.answer()
    await show_settings(call.message)


@dp.callback_query(F.data == "cfg:default")
async def cfg_default(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    await settings.update_one(
        {"_id": "post"},
        {"$set": {
            "caption": DEFAULT_CAPTION,
            "caption_entities": [],
            "button": DEFAULT_BUTTON,
            "media_type": None,
            "media_file_id": None,
        }},
        upsert=True,
    )
    invalidate_config_cache()
    await call.answer("✅ Successfully reset all to defaults")
    await show_settings(call.message)


@dp.callback_query(F.data.in_({"cfg:media", "cfg:caption", "cfg:button"}))
async def cfg_prompt(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    await ui_state.update_one(
        {"_id": f"prompt:{call.from_user.id}"},
        {"$set": {
            "action": action,
            "panel_chat_id": call.message.chat.id,
            "panel_message_id": call.message.message_id,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    prompts = {
        "media": "<i>Send new media for the links post. Only GIF and picture are supported.</i>",
        "caption": "<i>Send new caption for the Link Post.</i>",
        "button": "<i>Send new button text for the Link Post Button.</i>",
    }
    await call.answer()
    await edit_panel(
        call.message,
        prompts[action],
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data="settings")]
        ]),
    )


@dp.message(F.text.startswith("/"))
async def unknown_owner_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    old_panel_ids = await clear_owner_panels(message.chat.id, message.from_user.id)
    await render_home(message)
    fresh = await ui_state.find_one({"_id": f"home:{message.chat.id}"})
    await finish_owner_command(message, old_panel_ids, fresh.get("message_id") if fresh else None)


@dp.message()
async def owner_input(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    pending = await ui_state.find_one({"_id": f"prompt:{message.from_user.id}"})
    if not pending:
        return
    action = pending["action"]
    if action == "caption":
        text_value = message.text or message.caption
        if not text_value:
            await message.answer("Please send text for the caption.")
            return
        cfg_now = await get_config()
        caption_limit = 1024 if cfg_now.get("media_file_id") else 4096
        if len(text_value) > caption_limit:
            await message.answer(
                f"Caption is too long. Telegram allows up to {caption_limit} characters "
                "for the current post format."
            )
            return
        entities = message.entities if message.text else message.caption_entities
        await settings.update_one(
            {"_id": "post"},
            {"$set": {
                "caption": text_value,
                "caption_entities": [e.model_dump(mode="json", exclude_none=True) for e in (entities or [])],
            }},
            upsert=True,
        )
        invalidate_config_cache()
    elif action == "button":
        if not message.text:
            await message.answer("Please send the new button text as a text message.")
            return
        if len(message.text) > 64:
            await message.answer("Telegram inline button text must be 1–64 characters. Send a shorter label.")
            return
        await settings.update_one(
            {"_id": "post"}, {"$set": {"button": message.text}}, upsert=True
        )
        invalidate_config_cache()
    elif action == "media":
        media_type, file_id = None, None
        if message.photo:
            media_type, file_id = "photo", message.photo[-1].file_id
        elif message.animation:
            media_type, file_id = "animation", message.animation.file_id
        if not file_id:
            await message.answer("Please send only a picture or GIF.")
            return
        await settings.update_one(
            {"_id": "post"},
            {"$set": {"media_type": media_type, "media_file_id": file_id}},
            upsert=True,
        )
        invalidate_config_cache()
    else:
        await ui_state.delete_one({"_id": f"prompt:{message.from_user.id}"})
        return

    await ui_state.delete_one({"_id": f"prompt:{message.from_user.id}"})
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    try:
        await BOT.edit_message_text(
            "<b><u>LINKS POST</u></b>\n\n"
            "<i>Edit caption, media, and button using the buttons below:</i>",
            chat_id=pending["panel_chat_id"],
            message_id=pending["panel_message_id"],
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


async def telegram_webhook(request):
    secret = request.app.get("webhook_secret")
    if not secret or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        raise web.HTTPUnauthorized()
    try:
        from aiogram.types import Update
        update = Update.model_validate(await request.json(), context={"bot": BOT})
        await dp.feed_update(BOT, update)
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
    global BOT, bot_username, bot_user_id
    BOT = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    runner = None
    try:
        me = await BOT.get_me()
        bot_username = me.username or ""
        bot_user_id = me.id
        await mongo.admin.command("ping")
        await get_config()
        await chats.create_index("code", unique=True, sparse=True, name="uniq_chat_deepcode")
        await chats.create_index([("chat_id", 1)], unique=True, name="uniq_chat_id")
        await issued.create_index([("expires_at", 1), ("revoked", 1)], name="invite_expiry")
        await issued.create_index([("chat_id", 1), ("user_id", 1), ("created_at", -1)], name="invite_user_lookup")

        # Restore one independent timer per outstanding invite after a process restart.
        async for record in issued.find({"revoked": False}):
            schedule_expiry(record)

        public_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)
        app = web.Application()
        app["webhook_secret"] = webhook_secret
        app.router.add_get("/", health)
        app.router.add_get("/health", health)

        if public_url:
            app.router.add_post("/telegram/webhook", telegram_webhook)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000")))
            await site.start()
            await BOT.set_webhook(
                url=f"{public_url}/telegram/webhook",
                secret_token=webhook_secret,
                allowed_updates=["message", "channel_post", "callback_query", "chat_member", "chat_join_request"],
                drop_pending_updates=False,
            )
            log.info("Webhook active at %s/telegram/webhook; polling disabled.", public_url)
            await asyncio.Event().wait()
        else:
            log.warning("RENDER_EXTERNAL_URL missing; using polling fallback.")
            await dp.start_polling(
                BOT,
                allowed_updates=["message", "channel_post", "callback_query", "chat_member", "chat_join_request"],
            )
    finally:
        for task in list(expiry_tasks.values()):
            task.cancel()
        if expiry_tasks:
            await asyncio.gather(*list(expiry_tasks.values()), return_exceptions=True)
        if runner:
            await runner.cleanup()
        await BOT.session.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
