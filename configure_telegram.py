"""Save a verified BotFather token locally without displaying it."""
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import urllib.request


def main():
    token = getpass.getpass("Paste the Telegram bot token from BotFather: ").strip()
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token):
        raise SystemExit("The token format is invalid. Copy the complete token from BotFather.")
    try:
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe", data=b"")
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
        if not result.get("ok") or not result.get("result", {}).get("is_bot"):
            raise ValueError
    except Exception:
        raise SystemExit("Could not verify the bot. Check the token and internet connection.") from None
    path = Path(__file__).with_name(".env")
    content = path.read_text() if path.exists() else ""
    if re.search(r"(?m)^TELEGRAM_BOT_TOKEN=", content):
        content = re.sub(r"(?m)^TELEGRAM_BOT_TOKEN=.*$", "TELEGRAM_BOT_TOKEN=" + token, content)
    else:
        content = content.rstrip() + "\nTELEGRAM_BOT_TOKEN=" + token + "\n"
    temporary = path.with_name(".env." + secrets.token_hex(6))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(content)
    temporary.replace(path)
    print("Connected bot: https://t.me/" + result["result"]["username"])
    print("Run docker compose up --build -d to load the token.")


if __name__ == "__main__":
    main()
