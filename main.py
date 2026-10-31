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
OWNER = int(os.environ["OWNER"])
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

DEFAULT_CAPTION = "ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ! ᴄʟɪᴄᴋ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴘʀᴏᴄᴇᴇᴅ:"
DEFAULT_BUTTON = "ᴄʟɪᴄᴋ ʜᴇʀᴇ"
BOT: Bot
dp = Dispatcher()
bot_username = ""


def is_owner(user_id: int) -> bool:
    return user_id == OWNER


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
    ])


def detail_keyboard(code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ʀᴇᴍᴏᴠᴇ", callback_data=f"remove:{code}")],
        [InlineKeyboardButton(text="Back", callback_data="home")],
    ])


async def get_config():
    defaults = {
        "_id": "post", "caption": DEFAULT_CAPTION, "caption_entities": [],
        "button": DEFAULT_BUTTON, "media_type": None, "media_file_id": None,
    }
    await settings.update_one({"_id": "post"}, {"$setOnInsert": defaults}, upsert=True)
    return await settings.find_one({"_id": "post"})


async def home_text():
    return ("<b><u>ROTATION LINKS CHATS</u></b>\n\n"
            "<i>Select any chat for links delivery. A new link will be used every time to avoid spammers.</i>")


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
    cfg = await get_config()
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
            bot_administrator_rights={
                "can_invite_users": True,
                "can_manage_chat": True,
            },
        ),
    )
    return ReplyKeyboardMarkup(
        keyboard=[[button]], resize_keyboard=True, one_time_keyboard=True
    )


async def send_fresh_link(message: Message, chat_doc):
    invite = None
    try:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        invite = await BOT.create_chat_invite_link(
            chat_id=chat_doc["chat_id"],
            expire_date=expires_at,
            creates_join_request=bool(chat_doc.get("is_private")),
            name=f"LS-{message.from_user.id}"[:32],
        )
        record = {
            "invite_link": invite.invite_link,
            "chat_id": chat_doc["chat_id"],
            "code": chat_doc["code"],
            "user_id": message.from_user.id,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
            "revoked": False,
        }
        await issued.insert_one(record)
        cfg = await get_config()
        button_text = (cfg.get("button") or DEFAULT_BUTTON)[:64]
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, url=invite.invite_link)
        ]])
        caption = cfg.get("caption", DEFAULT_CAPTION)
        entities = cfg.get("caption_entities") or None
        media_id, media_type = cfg.get("media_file_id"), cfg.get("media_type")
        kwargs = {"caption": caption, "reply_markup": markup}
        if entities:
            kwargs["caption_entities"] = entities
            kwargs["parse_mode"] = None
        else:
            kwargs["parse_mode"] = None
        if media_id and media_type == "photo":
            await message.answer_photo(media_id, **kwargs)
        elif media_id and media_type == "animation":
            await message.answer_animation(media_id, **kwargs)
        else:
            await message.answer(caption, entities=entities, parse_mode=None,
                                 reply_markup=markup)
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
    if is_owner(message.from_user.id):
        await render_home(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    doc = await chats.find_one({"code": code, "active": True}) if code else None
    if not doc:
        await message.answer("This link is invalid or no longer available.")
        return
    await send_fresh_link(message, doc)


@dp.message(Command("set"))
async def set_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    panel = await ui_state.find_one({"_id": f"settings:{message.chat.id}"})
    target_id = (panel or {}).get("message_id")
    if target_id:
        try:
            await BOT.edit_message_text(
                "<b><u>LINKS POST</u></b>\n\n"
                "<i>Edit caption, media, and button using the buttons below:</i>",
                chat_id=message.chat.id, message_id=target_id,
                reply_markup=settings_keyboard(),
            )
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(
        "<b><u>LINKS POST</u></b>\n\n"
        "<i>Edit caption, media, and button using the buttons below:</i>",
        reply_markup=settings_keyboard(),
    )
    await ui_state.update_one({"_id": f"settings:{message.chat.id}"},
        {"$set": {"message_id": sent.message_id}}, upsert=True)


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
    await call.answer()
    await edit_panel(call.message, "<b>ADD CHAT</b>\n\n<i>Choose the chat type.</i>", add_keyboard())


@dp.callback_query(F.data.startswith("pick:"))
async def pick_chat(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    kind = call.data.split(":", 1)[1]
    if kind not in {"channel", "group"}:
        await call.answer("Invalid chat type", show_alert=True)
        return
    await call.answer()
    await edit_panel(
        call.message,
        f"<b>SELECT {kind.upper()}</b>\n\n"
        "<i>Use Telegram's chat picker below. The bot needs invite-link permission.</i>",
    )
    await call.message.answer("Choose a chat:", reply_markup=request_chat_keyboard(kind))


@dp.message(F.chat_shared)
async def chat_shared(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    try:
        shared = message.chat_shared
        chat = await BOT.get_chat(shared.chat_id)
        me = await BOT.get_me()
        member = await BOT.get_chat_member(shared.chat_id, me.id)
        if member.status not in {"administrator", "creator"}:
            await message.answer(
                "The bot must be an administrator in this chat with invite-link permission.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if not getattr(member, "can_invite_users", False) and member.status != "creator":
            await message.answer(
                "Please grant the bot permission to invite users, then select the chat again.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        code = "".join(secrets.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(25))
        title = chat.title or getattr(shared, "title", None) or str(shared.chat_id)
        await chats.update_one(
            {"chat_id": shared.chat_id},
            {"$set": {
                "chat_id": shared.chat_id,
                "title": title,
                "code": code,
                "username": getattr(chat, "username", None),
                "is_private": not bool(getattr(chat, "username", None)),
                "active": True,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        await message.answer("Chat added.", reply_markup=ReplyKeyboardRemove())
        await render_home(message)
    except Exception:
        log.exception("Unable to save selected chat")
        await message.answer(
            "Could not add this chat. Ensure the bot is an administrator with invite-link permission.",
            reply_markup=ReplyKeyboardRemove(),
        )


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


@dp.callback_query(F.data.startswith("remove:"))
async def remove_chat(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Not allowed", show_alert=True)
        return
    code = call.data.split(":", 1)[1]
    chat_doc = await chats.find_one({"code": code, "active": True})
    if not chat_doc:
        await call.answer("Chat not found", show_alert=True)
        return

    # Disable the deep link first so no new invites can be requested while removal runs.
    await chats.update_one({"_id": chat_doc["_id"]}, {"$set": {"active": False}})
    failed = False
    cursor = issued.find({"chat_id": chat_doc["chat_id"], "revoked": False})
    async for record in cursor:
        if not await revoke_record(record):
            failed = True

    if failed:
        # Keep the chat record so the owner can retry; successfully revoked links stay revoked.
        await chats.update_one({"_id": chat_doc["_id"]}, {"$set": {"active": True}})
        await call.answer(
            "Some links could not be revoked. Chat kept; please retry Remove.",
            show_alert=True,
        )
        return

    await chats.delete_one({"_id": chat_doc["_id"]})
    await call.answer("Chat removed; all active links revoked.")
    await render_home(call.message, edit_existing=True)


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
    else:
        await ui_state.delete_one({"_id": f"prompt:{message.from_user.id}"})
        return

    await ui_state.delete_one({"_id": f"prompt:{message.from_user.id}"})
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    try:
        panel = await BOT.get_chat(pending["panel_chat_id"])
        await BOT.edit_message_text(
            "<b><u>LINKS POST</u></b>\n\n"
            "<i>Edit caption, media, and button using the buttons below:</i>",
            chat_id=panel.id,
            message_id=pending["panel_message_id"],
            reply_markup=settings_keyboard(),
        )
    except TelegramAPIError:
        log.exception("Could not restore settings panel")


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
    if record:
        await revoke_record(record)


async def revoke_record(record):
    try:
        await BOT.revoke_chat_invite_link(record["chat_id"], record["invite_link"])
    except TelegramBadRequest as exc:
        # Telegram also returns BadRequest when a link has already expired or been revoked.
        log.info("Invite already expired/revoked: %s", exc)
    except TelegramAPIError:
        log.exception("Invite revoke failed for chat %s", record["chat_id"])
        return False
    await issued.update_one(
        {"_id": record["_id"]},
        {"$set": {"revoked": True, "revoked_at": datetime.now(timezone.utc)}},
    )
    return True


async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            cursor = issued.find(
                {"revoked": False, "expires_at": {"$lte": now}}
            ).limit(500)
            async for record in cursor:
                await revoke_record(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Expiry worker error")
        await asyncio.sleep(5)


async def health(request):
    try:
        await mongo.admin.command("ping")
        return web.json_response({"status": "ok", "service": "Link-Share", "database": "connected"})
    except Exception:
        log.exception("Health check database ping failed")
        return web.json_response({"status": "degraded", "service": "Link-Share"}, status=503)


async def main():
    global BOT, bot_username
    BOT = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    worker = None
    runner = None
    try:
        me = await BOT.get_me()
        bot_username = me.username or ""
        await mongo.admin.command("ping")
        await get_config()
        await chats.create_index("code", unique=True, sparse=True, name="uniq_chat_deepcode")
        await chats.create_index([("chat_id", 1)], unique=True, name="uniq_chat_id")
        await issued.create_index([("expires_at", 1), ("revoked", 1)], name="invite_expiry")
        await issued.create_index([("chat_id", 1), ("user_id", 1), ("created_at", -1)], name="invite_user_lookup")
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000")))
        await site.start()
        worker = asyncio.create_task(expiry_worker())
        log.info("Link-Share ready as @%s; MongoDB connected; HTTP health server listening.", bot_username)
        await dp.start_polling(
            BOT,
            allowed_updates=["message", "callback_query", "chat_member", "chat_join_request"],
        )
    finally:
        if worker:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        if runner:
            await runner.cleanup()
        await BOT.session.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
