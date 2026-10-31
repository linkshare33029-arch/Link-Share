# Link-Share Telegram Bot

Aiogram 3 bot with MongoDB Atlas storage and Render hosting.

## Render settings
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`
- Runtime: Python
- Environment variables:
  - `BOT_TOKEN`: Telegram bot token
  - `OWNER`: numeric Telegram owner ID
  - `URI`: full MongoDB Atlas SRV connection string (include the database username and password; keep it secret)
  - `DB_NAME`: optional, defaults to `link_share`

## Telegram setup
The bot must be an administrator in every selected channel/group with permission to create invite links. The owner uses Telegram's native chat picker to choose a channel or group; the bot does not automatically enumerate the owner's entire Telegram chat list.

Public chats receive one-minute invite links. Private chats receive join-request links with a one-minute expiry. Links are revoked on expiry and after Telegram reports a successful join/approval.

Owner panels are edited in place. The owner can use `/start` for the chat panel and `/set` for link-post settings. Captions preserve Telegram message entities; inline keyboard button labels cannot carry Telegram formatting, so the default button uses Unicode small-cap characters.
