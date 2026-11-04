from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base


class Database:
    def __init__(self, url: str):
        self.sqlite = url.startswith("sqlite")
        self.engine = create_async_engine(url, echo=False, hide_parameters=True, **(
            {"connect_args": {"timeout": 30}} if self.sqlite else {"pool_pre_ping": True}
        ))
        if self.sqlite:
            @event.listens_for(self.engine.sync_engine, "connect")
            def sqlite_setup(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def write(self):
        """Serialize DEMO writes across processes, not just asyncio tasks.

        Coarse locking is intentional for this small shop. SQLite reserves its
        writer before any read; PostgreSQL uses a transaction advisory lock.
        All business writes must enter this context. Exceptions roll back.
        """
        async with self.sessions() as session:
            try:
                if self.sqlite:
                    await session.execute(text("BEGIN IMMEDIATE"))
                else:
                    await session.begin()
                    await session.execute(text("SELECT pg_advisory_xact_lock(714930210)"))
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def create_schema(self):
        """Test helper. Production schema changes use Alembic migrations."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self):
        await self.engine.dispose()
