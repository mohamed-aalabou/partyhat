"""Neon Postgres DB layer: async engine, session, and table creation."""

import os

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agents.db.models import Base

load_dotenv()

# Neon connection string: use postgresql+asyncpg for SQLAlchemy async
# If DATABASE_URL is postgresql://..., convert to postgresql+asyncpg://
# asyncpg uses connect_args["ssl"], not URL params like sslmode.
def _get_async_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url or "postgresql+asyncpg://localhost/partyhat"


def _is_remote_ssl_host(url: str) -> bool:
    """True if URL points to a host that requires SSL (e.g. Neon)."""
    if not url or "localhost" in url or "127.0.0.1" in url:
        return False
    return "neon.tech" in url or ".aws.neon.tech" in url


_db_url = _get_async_url()
_engine = create_async_engine(
    _db_url,
    echo=os.getenv("SQL_ECHO", "").lower() in ("1", "true", "yes"),
    pool_pre_ping=True,  # check connection before use (avoids "connection is closed")
    pool_recycle=300,    # recycle connections before Neon idle timeout
    connect_args={"ssl": True} if _is_remote_ssl_host(_db_url) else {},
)

async_session_factory = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session():
    """Yield an async session for dependency injection. Yields None when DATABASE_URL is not set."""
    if not os.getenv("DATABASE_URL"):
        yield None
        return
    async with async_session_factory() as session:
        yield session


async def _migrate_users_email_to_wallet(conn) -> None:
    """Rename users.email -> users.wallet if the email column exists (one-off migration)."""
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'email'
              ) THEN
                ALTER TABLE users RENAME COLUMN email TO wallet;
              END IF;
            END $$;
            """
        )
    )


async def create_tables() -> None:
    """Create all tables. Call on app startup or migrations."""
    if not os.getenv("DATABASE_URL"):
        return  # Skip when DATABASE_URL not configured (e.g. local dev without Neon)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_users_email_to_wallet(conn)


async def drop_tables() -> None:
    """Drop all tables. For tests or reset only."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
