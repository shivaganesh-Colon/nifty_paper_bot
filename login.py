"""
Daily login for Kite Connect. Run once every trading morning before bot.py
(SEBI rules require a fresh 2FA login each day).

1. Run:  python login.py
2. Open the printed URL, log in with your Zerodha ID, password and TOTP.
3. You are redirected to your app's redirect URL. Copy the value of
   request_token=... from the address bar and paste it here.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

import config as C

IST = ZoneInfo("Asia/Kolkata")


def main():
    kite = KiteConnect(api_key=C.API_KEY)
    print("\nOpen this URL and log in:\n")
    print(kite.login_url())
    request_token = input("\nPaste request_token here: ").strip()
    if "request_token=" in request_token:  # user pasted the whole URL
        request_token = request_token.split("request_token=")[1].split("&")[0]

    session = kite.generate_session(request_token, api_secret=C.API_SECRET)
    with open("access_token.json", "w") as f:
        json.dump({"date": datetime.now(IST).date().isoformat(),
                   "access_token": session["access_token"]}, f)
    print(f"\nLogged in as {session.get('user_name', '')}. Token saved for today.")


if __name__ == "__main__":
    main()
