"""Neon Postgres DB layer: async engine, session, and table creation."""

import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agents.db.models import Base

load_dotenv()

# Neon connection string: use postgresql+asyncpg for SQLAlchemy async
# If DATABASE_URL is postgresql://..., convert to postgresql+asyncpg://
def _get_async_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql+asyncpg://"):
        return url
    return url or "postgresql+asyncpg://localhost/partyhat"


_engine = create_async_engine(
    _get_async_url(),
    echo=os.getenv("SQL_ECHO", "").lower() in ("1", "true", "yes"),
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


async def create_tables() -> None:
    """Create all tables. Call on app startup or migrations."""
    if not os.getenv("DATABASE_URL"):
        return  # Skip when DATABASE_URL not configured (e.g. local dev without Neon)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables() -> None:
    """Drop all tables. For tests or reset only."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
