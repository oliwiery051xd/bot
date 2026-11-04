import asyncio

from app.config import Settings
from app.database import Database
from app.services.shop import ShopService


async def seed():
    settings = Settings()
    db = Database(settings.database_url)
    try:
        await ShopService(db, settings).seed()
        print("MONSTAL: katalog DEMO gotowy. Istniejące produkty pozostawiono bez zmian." if settings.demo_mode
              else "MONSTAL: baza prawdziwych sald PLN gotowa. Dodano dwa nieaktywne wpisy przykładowe do konfiguracji.")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
