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
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, ChatJoinRequest, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, KeyboardButtonRequestChat,
    Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("link-share")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER"])
MONGO_URI = os.environ.get("URI", "").strip()
DB_USER = os.environ.get("USER", "").strip()
DB_NAME = os.environ.get("DB_NAME", "link_share")

if not MONGO_URI:
    raise RuntimeError("Set the complete MongoDB connection string in Render env var URI.")
mongo = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=15000)
db = mongo[DB_NAME]
chats = db.chats
settings = db.settings
issued = db.issued_links

DEFAULT_CAPTION = "Here is your link! Click button below to proceed:"
DEFAULT_BUTTON = "Click here"
BOT: Bot
dp = Dispatcher(storage=MemoryStorage())
owner_prompts = {}
owner_panels = {}
bot_username = ""


def is_owner(user_id: int) -> bool:
    return user_id == OWNER


def main_keyboard(items):
    rows, row = [], []
    for item in items:
        row.append(InlineKeyboardButton(text=item["title"][:64], callback_data=f"chat:{item['code']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="+", callback_data="add")])
    rows.append([InlineKeyboardButton(text="LINKS POST", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_home(message: Message, edit=True):
    items = await chats.find({"active": True}).sort("created_at", 1).to_list(length=None)
    body = "<b><u>ROTATION LINKS CHATS</u></b>\n\n<i>Select a chat below or add a new one.</i>"
    markup = main_keyboard(items)
    if edit:
        try:
            await message.edit_text(body, reply_markup=markup)
            return message
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return message
            log.info("Home panel edit fallback: %s", e)
    sent = await message.answer(body, reply_markup=markup)
    owner_panels[message.chat.id] = sent.message_id
    return sent


async def edit_owner_panel(message: Message, text: str, markup=None):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            log.warning("Could not edit owner panel: %s", e)


def detail_keyboard(code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Remove", callback_data=f"remove:{code}")],
        [InlineKeyboardButton(text="Back", callback_data="home")],
    ])


def settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Default", callback_data="cfg:default"),
         InlineKeyboardButton(text="Media", callback_data="cfg:media")],
        [InlineKeyboardButton(text="Caption", callback_data="cfg:caption"),
         InlineKeyboardButton(text="Button", callback_data="cfg:button")],
        [InlineKeyboardButton(text="Back", callback_data="home")],
    ])


async def get_config():
    doc = await settings.find_one({"_id": "post"})
    if not doc:
        doc = {"_id": "post", "caption": DEFAULT_CAPTION, "caption_entities": [],
               "button": DEFAULT_BUTTON, "media_type": None, "media_file_id": None}
        await settings.insert_one(doc)
    return doc


async def show_settings(message: Message):
    cfg = await get_config()
    body = ("<b>LINKS POST</b>\n\n"
            "<i>Configure the message users receive with their one-minute invite link.</i>\n\n"
            f"Caption: {html.escape(cfg.get('caption', DEFAULT_CAPTION))[:500]}\n"
            f"Button: {html.escape(cfg.get('button', DEFAULT_BUTTON))}\n"
            f"Media: {'Set' if cfg.get('media_file_id') else 'Not set'}")
    await edit_owner_panel(message, body, settings_keyboard())


def request_chat_keyboard(kind: str):
    req_id = secrets.randbelow(2_000_000_000) + 1
    channel = kind == "channel"
    button = KeyboardButton(
        text=f"Choose {kind.title()}",
        request_chat=KeyboardButtonRequestChat(
            request_id=req_id,
            chat_is_channel=channel,
            request_title=True,
            request_username=True,
            bot_administrator_rights={"can_invite_users": True},
        ),
    )
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True, one_time_keyboard=True)


@dp.message(CommandStart())
async def start(message: Message):
    if is_owner(message.from_user.id):
        await render_home(message, edit=False)
        return
    payload = (message.text or "").split(maxsplit=1)
    code = payload[1].strip() if len(payload) > 1 else ""
    chat_doc = await chats.find_one({"code": code, "active": True}) if code else None
    if not chat_doc:
        await message.answer("This link is invalid or no longer available.")
        return
    await send_fresh_link(message, chat_doc)


async def send_fresh_link(message: Message, chat_doc):
    try:
        expire_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        invite = await BOT.create_chat_invite_link(
            chat_id=chat_doc["chat_id"],
            expire_date=expire_at,
            creates_join_request=bool(chat_doc.get("is_private", False)),
            name=f"LinkShare {message.from_user.id}"[:32],
        )
        record = {
            "invite_link": invite.invite_link,
            "chat_id": chat_doc["chat_id"],
            "code": chat_doc["code"],
            "user_id": message.from_user.id,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expire_at,
            "revoked": False,
        }
        await issued.insert_one(record)
        cfg = await get_config()
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=cfg.get("button") or DEFAULT_BUTTON, url=invite.invite_link)
        ]])
        caption = cfg.get("caption", DEFAULT_CAPTION)
        entities = cfg.get("caption_entities") or None
        media_id, media_type = cfg.get("media_file_id"), cfg.get("media_type")
        if media_id and media_type == "photo":
            await message.answer_photo(media_id, caption=caption, caption_entities=entities, parse_mode=None if entities else ParseMode.HTML, reply_markup=markup)
        elif media_id and media_type == "animation":
            await message.answer_animation(media_id, caption=caption, caption_entities=entities, parse_mode=None if entities else ParseMode.HTML, reply_markup=markup)
        elif media_id and media_type == "video":
            await message.answer_video(media_id, caption=caption, caption_entities=entities, parse_mode=None if entities else ParseMode.HTML, reply_markup=markup)
        elif media_id and media_type == "document":
            await message.answer_document(media_id, caption=caption, caption_entities=entities, parse_mode=None if entities else ParseMode.HTML, reply_markup=markup)
        else:
            await message.answer(caption, entities=entities, parse_mode=None if entities else ParseMode.HTML, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.exception("Invite creation/send failed for chat %s", chat_doc.get("chat_id"))
        await message.answer("I couldn't create an invite for this chat. Please ask the owner to check that I am an administrator with invite-link permission.")
    except Exception:
        log.exception("Unexpected link delivery error")
        await message.answer("Something went wrong while creating your link. Please try again later.")


@dp.callback_query(F.data == "home")
async def go_home(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    await call.answer()
    await render_home(call.message)


@dp.callback_query(F.data == "add")
async def add_menu(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Channel", callback_data="pick:channel"),
         InlineKeyboardButton(text="Group", callback_data="pick:group")],
        [InlineKeyboardButton(text="Back", callback_data="home")],
    ])
    await call.answer()
    await edit_owner_panel(call.message, "<b>ADD CHAT</b>\n\n<i>Choose the chat type.</i>", kb)


@dp.callback_query(F.data.startswith("pick:"))
async def pick_chat(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    kind = call.data.split(":", 1)[1]
    await call.answer()
    await edit_owner_panel(call.message,
        f"<b>SELECT {kind.upper()}</b>\n\n<i>Tap the button below and choose a chat from Telegram's chat picker. The bot must be added as an administrator with invite-link permission.</i>")
    await call.message.answer("Choose a chat:", reply_markup=request_chat_keyboard(kind))


@dp.message(F.chat_shared)
async def chat_shared(message: Message):
    if not is_owner(message.from_user.id):
        return
    shared = message.chat_shared
    try:
        chat = await BOT.get_chat(shared.chat_id)
        member = await BOT.get_chat_member(shared.chat_id, (await BOT.get_me()).id)
        if member.status not in ("administrator", "creator"):
            await message.answer("Please add/promote the bot as an administrator, then select the chat again.", reply_markup=ReplyKeyboardRemove())
            return
        code = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
        title = chat.title or shared.title or str(shared.chat_id)
        await chats.update_one(
            {"chat_id": shared.chat_id},
            {"$set": {"chat_id": shared.chat_id, "title": title, "code": code,
                      "username": getattr(chat, "username", None), "is_private": not bool(getattr(chat, "username", None)),
                      "active": True, "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        await message.answer("Chat added.", reply_markup=ReplyKeyboardRemove())
        panel_id = owner_panels.get(message.chat.id)
        if panel_id:
            try:
                items = await chats.find({"active": True}).sort("created_at", 1).to_list(length=None)
                await BOT.edit_message_text(
                    "<b><u>ROTATION LINKS CHATS</u></b>\\n\\n<i>Select a chat below or add a new one.</i>",
                    chat_id=message.chat.id, message_id=panel_id, reply_markup=main_keyboard(items)
                )
            except TelegramBadRequest as e:
                log.info("Could not refresh owner panel: %s", e)
    except Exception:
        log.exception("Unable to save selected chat")
        await message.answer("Could not add this chat. Ensure the bot is an admin and try again.", reply_markup=ReplyKeyboardRemove())


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
    link = f"https://t.me/{bot_username}?start={code}"
    text = f"<i>{html.escape(item['title'])}</i>\n\n<code>{link}</code>"
    await call.answer()
    await edit_owner_panel(call.message, text, detail_keyboard(code))


@dp.callback_query(F.data.startswith("remove:"))
async def remove_chat(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    code = call.data.split(":", 1)[1]
    await chats.update_one({"code": code}, {"$set": {"active": False}})
    await call.answer("Removed")
    await render_home(call.message)


@dp.callback_query(F.data == "settings")
async def open_settings(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    await call.answer()
    await show_settings(call.message)


@dp.callback_query(F.data == "cfg:default")
async def cfg_default(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    await settings.update_one({"_id": "post"}, {"$set": {
        "caption": DEFAULT_CAPTION, "caption_entities": [], "button": DEFAULT_BUTTON,
        "media_type": None, "media_file_id": None}}, upsert=True)
    await call.answer("Defaults restored")
    await show_settings(call.message)


@dp.callback_query(F.data.in_({"cfg:media", "cfg:caption", "cfg:button"}))
async def cfg_prompt(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    owner_prompts[call.from_user.id] = {"action": action, "panel_chat_id": call.message.chat.id,
                                        "panel_message_id": call.message.message_id}
    await call.answer()
    prompts = {
        "media": "<b>Send a GIF, picture, video, or document.</b>\n\n<i>Send it here; the file will be used in future link posts.</i>",
        "caption": "<b>Send the new caption.</b>\n\n<i>Telegram formatting and entities from your message will be saved.</i>",
        "button": "<b>Send the new button text.</b>",
    }
    await edit_owner_panel(call.message, prompts[action], InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cancel", callback_data="settings")]
    ]))


@dp.message()
async def owner_input(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    pending = owner_prompts.get(message.from_user.id)
    if not pending:
        return
    action = pending["action"]
    if action == "caption":
        if not message.text and not message.caption:
            await message.answer("Please send text for the caption.")
            return
        text_value = message.text or message.caption
        entities = message.entities if message.text else message.caption_entities
        await settings.update_one({"_id": "post"}, {"$set": {
            "caption": text_value,
            "caption_entities": [e.model_dump(exclude_none=True) for e in (entities or [])]
        }}, upsert=True)
    elif action == "button":
        if not message.text:
            await message.answer("Please send plain text for the button.")
            return
        await settings.update_one({"_id": "post"}, {"$set": {"button": message.text[:64]}}, upsert=True)
    elif action == "media":
        media_type, file_id = None, None
        if message.photo:
            media_type, file_id = "photo", message.photo[-1].file_id
        elif message.animation:
            media_type, file_id = "animation", message.animation.file_id
        elif message.video:
            media_type, file_id = "video", message.video.file_id
        elif message.document:
            media_type, file_id = "document", message.document.file_id
        if not file_id:
            await message.answer("Please send a picture, GIF, video, or document.")
            return
        await settings.update_one({"_id": "post"}, {"$set": {
            "media_type": media_type, "media_file_id": file_id}}, upsert=True)
    owner_prompts.pop(message.from_user.id, None)
    try:
        await message.delete()
    except Exception:
        pass
    try:
        cfg = await get_config()
        body = ("<b>LINKS POST</b>\\n\\n"
                "<i>Configure the message users receive with their one-minute invite link.</i>\\n\\n"
                f"Caption: {html.escape(cfg.get('caption', DEFAULT_CAPTION))[:500]}\\n"
                f"Button: {html.escape(cfg.get('button', DEFAULT_BUTTON))}\\n"
                f"Media: {'Set' if cfg.get('media_file_id') else 'Not set'}")
        await BOT.edit_message_text(body, chat_id=pending["panel_chat_id"],
                                    message_id=pending["panel_message_id"],
                                    reply_markup=settings_keyboard())
    except Exception:
        log.exception("Could not restore settings panel")


@dp.chat_join_request()
async def join_request(update: ChatJoinRequest):
    # Keep a pending request's invite alive until the request is approved or expires.
    pass


@dp.chat_member()
async def member_update(update: ChatMemberUpdated):
    if update.new_chat_member.status not in ("member", "administrator", "creator"):
        return
    link = update.invite_link.invite_link if update.invite_link else None
    if not link:
        return
    record = await issued.find_one({"invite_link": link, "revoked": False})
    if record:
        await revoke_record(record)


async def revoke_record(record):
    try:
        await BOT.revoke_chat_invite_link(record["chat_id"], record["invite_link"])
    except TelegramBadRequest as e:
        log.info("Invite revoke response: %s", e)
    await issued.update_one({"_id": record["_id"]}, {"$set": {"revoked": True, "revoked_at": datetime.now(timezone.utc)}})


async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            expired = await issued.find({"revoked": False, "expires_at": {"$lte": now}}).to_list(length=500)
            for record in expired:
                await revoke_record(record)
        except Exception:
            log.exception("Expiry worker error")
        await asyncio.sleep(5)


async def health(request):
    return web.json_response({"status": "ok", "service": "Link-Share"})


async def main():
    global BOT, bot_username
    BOT = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await BOT.get_me()
    bot_username = me.username or ""
    await mongo.admin.command("ping")
    await get_config()
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000")))
    await site.start()
    worker = asyncio.create_task(expiry_worker())
    try:
        await dp.start_polling(BOT, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        await runner.cleanup()
        await BOT.session.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
