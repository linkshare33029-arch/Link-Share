import html

DEFAULT_ABOUT_TEXT = (
    "<b><i>✨ ᴀʙᴏᴜᴛ ᴍᴇ</i></b>\\n\\n"
    "<i>{bot_mention} makes it easy to discover and join the communities shared here. "
    "Choose an option on the Start page to receive an invitation, then open it in Telegram "
    "and follow the on-screen steps. Private communities may require a join request for approval. "
    "If an invitation expires, return to the original link to request a fresh one.</i>\\n\\n"
    "<b>✦ ɪɴғᴏʀᴍᴀᴛɪᴏɴ</b>\\n"
    "<b>➤ ɴᴀᴍᴇ : {bot_mention}</b>\\n"
    "<b>➤ ᴄʀᴇᴀᴛᴏʀ : ʏᴜᴛᴀ</b>\\n"
    "<b>➤ ᴏᴡɴᴇʀ : 𝓚𝔂ᴏᴋᴏ</b>\\n"
    "<b>➤ ᴍᴀᴅᴇ ғᴏʀ : {anime}</b>\\n"
    "<b>➤ ᴠᴇʀsɪᴏɴ : ✧ ᴠ 0.1 ⚡</b>"
)


def render_about(bot_info):
    bot_name = html.escape(bot_info.full_name)
    if bot_info.username:
        username = html.escape(bot_info.username, quote=True)
        bot_mention = f'<a href="https://t.me/{username}">{bot_name}</a>'
    else:
        bot_mention = bot_name
    anime = '<a href="http://t.me/lexianime">Lexi Anime</a>'
    return DEFAULT_ABOUT_TEXT.format(bot_name=bot_name, bot_mention=bot_mention, anime=anime)
