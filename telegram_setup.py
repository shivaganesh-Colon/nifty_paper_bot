"""
One-time Telegram setup. Run:  python telegram_setup.py

Before running:
  1. In Telegram, search for  @BotFather  (blue tick) and open it.
  2. Send  /newbot , choose a name, then a username ending in 'bot'.
  3. BotFather replies with a TOKEN like 123456789:ABCdef...  - keep it ready.

This script then finds your chat ID, sends a test message and saves
everything into config.py for you.
"""
import re
import sys

import requests

import config as C

API = "https://api.telegram.org/bot{}/{}"


def call(token, method, **params):
    r = requests.get(API.format(token, method), params=params, timeout=15)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"HTTP {r.status_code}"))
    return data["result"]


def save_setting(name, value):
    with open("config.py", encoding="utf-8") as f:
        text = f.read()
    line = f"{name} = {value!r}"
    pat = re.compile(rf"^{name}\s*=.*$", re.M)
    if pat.search(text):
        text = pat.sub(lambda m: line, text, count=1)
    else:
        if "# ---------------- Telegram alerts" not in text:
            text = text.rstrip("\n") + "\n\n# ---------------- Telegram alerts ----------------\n"
        text = text.rstrip("\n") + "\n" + line + "\n"
    with open("config.py", "w", encoding="utf-8") as f:
        f.write(text)


def main():
    token = str(getattr(C, "TELEGRAM_BOT_TOKEN", "") or "")
    if not token or token.startswith("your_"):
        token = input("Paste the bot TOKEN from @BotFather: ").strip()

    try:
        me = call(token, "getMe")
    except Exception as e:
        sys.exit(f"\nThat token did not work ({e}). Copy it again from @BotFather and retry.")
    name = me["username"]
    print(f"\nToken OK - your bot is @{name}")
    print(f"\nNow, on your phone: open  https://t.me/{name}  (or search @{name} in Telegram),")
    print("tap START (or send 'hi'), then come back here.")
    input("Press Enter after you've sent the message... ")

    chats = {}
    for u in call(token, "getUpdates"):
        msg = u.get("message") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        sys.exit(f"\nNo message found yet. Send 'hi' to @{name} in Telegram, then run this again.")
    chat_id, who = list(chats.items())[-1]
    print(f"Found your chat: {who} (ID {chat_id})")

    try:
        call(token, "sendMessage", chat_id=chat_id,
             text="✅ NIFTY paper bot is connected. You'll get trade alerts here.")
    except Exception as e:
        sys.exit(f"\nCould not send the test message: {e}")

    save_setting("TELEGRAM_BOT_TOKEN", token)
    save_setting("TELEGRAM_CHAT_ID", str(chat_id))
    save_setting("TELEGRAM_ENABLED", True)
    print("\nTest message sent - check Telegram on your phone.")
    print("Saved token, chat ID and TELEGRAM_ENABLED = True into config.py. All done!")


if __name__ == "__main__":
    main()
