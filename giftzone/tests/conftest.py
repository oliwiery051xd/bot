import pytest_asyncio

from app.config import Settings
from app.database.core import Database
from app.services.shop import ShopService


ADMIN = 9001
ALICE = 101
BOB = 102
CHARLIE = 103


@pytest_asyncio.fixture
async def shop(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.create_schema()
    settings = Settings(
        _env_file=None,
        demo_mode=True,
        admin_ids=[ADMIN],
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        referral_bonus_grosze=1000,
    )
    service = ShopService(db, settings)
    await service.seed()
    yield service
    await db.dispose()


@pytest_asyncio.fixture
async def buyer(shop):
    return await shop.register(ALICE, "alice", "Alicja")


@pytest_asyncio.fixture
async def product(shop):
    categories = await shop.categories()
    products = await shop.products(categories[0].id)
    return products[0]


@pytest_asyncio.fixture
async def funded_buyer(shop, buyer):
    return await shop.admin_balance(ADMIN, ALICE, 100_000, "fixture-funds")
