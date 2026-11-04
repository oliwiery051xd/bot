"""Create and securely persist the Fernet key used to encrypt delivery details."""
from pathlib import Path
import os

from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key


def main():
    env_path = Path(".env")
    if not env_path.exists():
        raise SystemExit("Brak .env. Skopiuj .env.example do .env i uruchom ponownie.")
    if dotenv_values(env_path).get("CUSTOMER_DATA_KEY"):
        raise SystemExit("CUSTOMER_DATA_KEY już istnieje w .env; pozostawiono go bez zmian.")
    set_key(str(env_path), "CUSTOMER_DATA_KEY", Fernet.generate_key().decode(), quote_mode="never")
    os.chmod(env_path, 0o600)
    print("Utworzono CUSTOMER_DATA_KEY w .env. Zrób bezpieczną kopię zapasową .env.")


if __name__ == "__main__":
    main()
