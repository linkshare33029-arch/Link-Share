# Link-Share Telegram Bot

Aiogram 3 Telegram bot with MongoDB Atlas persistence and a Render HTTP health endpoint.

## Render settings

- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`
- Runtime: Python
- Environment variables:
  - `BOT_TOKEN`: Telegram bot token
  - `OWNER`: your numeric Telegram user ID
  - `URI`: MongoDB Atlas SRV URI with the database username and password
  - `DB_NAME`: optional; defaults to `link_share`

MongoDB Atlas database user confirmed in the project: `LinkShare` (case-sensitive). The separate user `linkshare33029_db_user` is an Atlas administrator account and is not the app user.

Use this URI template in Render's `URI` variable, replacing only `<PASSWORD>` with the password you already saved (URL-encode special characters in the password):

```
mongodb+srv://LinkShare:<PASSWORD>@link-share.r3ab63w.mongodb.net/?retryWrites=true&w=majority&appName=Link-Share
```

The bot uses database `link_share` and collections `chats`, `settings`, `issued_links`, and `owner_ui`. The first three collections already exist in the Atlas cluster; the bot creates its indexes and the owner state collection on startup.

## Telegram setup and behavior

- The owner uses `/start` for the chat panel and `/set` for post settings.
- The `+` menu opens Channel / Group selection. Telegram's Bot API does **not** let a bot enumerate every channel/group in an owner's account. The bot therefore uses Telegram's native chat picker: select one chat at a time. The bot must be an administrator with invite-link permission.
- Added chats appear as exact-title buttons, two per row, with no fixed chat-count limit imposed by the bot.
- Each chat gets its own deep-link code. A user opening that deep link receives a fresh invite for that selected chat only.
- Public chats get a one-minute invite link. Chats without a public username are treated as private and get a join-request invite with a one-minute expiry.
- The bot attempts to revoke each invite on expiry and immediately after Telegram reports the user joined/was approved.
- Owner panel IDs and pending settings prompts are persisted in MongoDB so they survive a process restart.
- Caption formatting is preserved using Telegram message entities. Telegram inline keyboard button labels do not support rich-text entities/formatting; button text is plain text (up to 64 characters). Unicode small-cap characters are used for the default label.
- Media supports Telegram photos and GIF animations only. The selected media, caption and invite button are sent together.

## Operational notes

- The bot needs to remain running to poll Telegram updates and revoke expired invites promptly.
- Render Free Web Services can spin down after inactivity. For continuous 24/7 polling, use a paid always-on Render service.
- Telegram can delay or omit some membership metadata; the bot uses the invite URL when available and a per-user/per-chat fallback when Telegram omits it. No bot can guarantee revocation at the exact millisecond if Telegram does not deliver the membership update immediately.
