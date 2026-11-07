"""Create a local environment file with generated credentials."""
import os
import secrets
from pathlib import Path


def main():
    path = Path(__file__).with_name(".env")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Local settings already exist. Start Docker and select Admin or Worker.")
        return
    with os.fdopen(descriptor, "w") as env_file:
        env_file.write("ADMIN_EMAIL=admin@oncoaid.local\n")
        env_file.write("ONE_CLICK_ACCESS=true\n")
        env_file.write(f"ADMIN_PASSWORD={secrets.token_urlsafe(24)}\n")
        env_file.write(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}\n")
    print("Created local settings. Start Docker and select Admin or Worker; no sign-in password is needed.")


if __name__ == "__main__":
    main()
