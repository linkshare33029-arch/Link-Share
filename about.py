import html

DEFAULT_ABOUT_TEXT = (
    "<i>✨ ᴀʙᴏᴜᴛ ᴍᴇ</i>\\n\\n"
    "<i>{bot_mention} helps users discover and join the communities shared through this bot. "
    "Choose an available option on the Start page to receive a fresh invitation. "
    "Open the invitation in Telegram and follow the on-screen steps to join. "
    "Private communities may require an additional join-request approval. "
    "If an invitation expires, return to the original link and request a new one.</i>\\n\\n"
    "<b>✦ ɪɴғᴏʀᴍᴀᴛɪᴏɴ</b>\\n"
    "<b>➤ ɴᴀᴍᴇ : {bot_mention}</b>\\n"
    "<b>➤ ᴄʀᴇᴀᴛᴏʀ : ʏᴜᴛᴀ</b>\\n"
    "<b>➤ ᴏᴡɴᴇʀ : 𝓚𝔂𝓸ᴋᴏ</b>\\n"
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
