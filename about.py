import html

DEFAULT_ABOUT_TEXT = (
    "<i>✦ <b>ᴀʙᴏᴜᴛ {bot_name}</b> ✦\n\n"
    "<b>{bot_name}</b> makes sharing access to configured groups and channels simple. "
    "Choose an available option on Start or open a {bot_name} invite link to get a fresh invite.\n\n"
    "<b>✦ Hᴏᴡ ɪᴛ ᴡᴏʀᴋs</b>\n"
    "Open the invite link in Telegram, then tap the button to join the selected group or channel. "
    "For private chats, users must receive approval before joining.\n\n"
    "<b>✦ ɪɴᴠɪᴛᴇ ɴᴏᴛᴇs</b>\n"
    "Invites expire in 2 minutes. Public-group links allow one member. "
    "If a link expires, open the original link again to get a new one.\n\n"
    "<b>✨ ᴀʙᴏᴜᴛ ᴍᴇ</b>\n\n"
    "<b>✦ ɪɴғᴏʀᴍᴀᴛɪᴏɴ</b>\n"
    "<b>➤ ɴᴀᴍᴇ : {bot_mention}</b>\n"
    "<b>➤ ᴄʀᴇᴀᴛᴏʀ : ʏᴜᴛᴀ</b>\n"
    "<b>➤ ᴏᴡɴᴇʀ : 𝓚𝔂ᴏᴋᴏ</b>\n"
    "<b>➤ ᴍᴀᴅᴇ ғᴏʀ : {anime}</b>\n"
    "<b>➤ ᴠᴇʀsɪᴏɴ : ✧ ᴠ 0.1 ⚡</b></i>"
)
def render_default_about(bot_info):
    bot_name = html.escape(bot_info.full_name)
    if bot_info.username:
        username = html.escape(bot_info.username, quote=True)
        bot_mention = f'<a href="https://t.me/{username}">{bot_name}</a>'
    else:
        bot_mention = bot_name
    anime = '<a href="http://t.me/lexianime">Lexi Anime</a>'
    return DEFAULT_ABOUT_TEXT.format(bot_name=bot_name, bot_mention=bot_mention, anime=anime)

def render_about(bot_info):
    bot_name = html.escape(bot_info.full_name)
    if bot_info.username:
        username = html.escape(bot_info.username, quote=True)
        bot_mention = f'<a href="https://t.me/{username}">{bot_name}</a>'
    else:
        bot_mention = bot_name
    anime = '<a href="http://t.me/lexianime">Lexi Anime</a>'
    return DEFAULT_ABOUT_TEXT.format(bot_name=bot_name, bot_mention=bot_mention, anime=anime)
