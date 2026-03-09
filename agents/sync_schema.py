#!/usr/bin/env python3
"""
Push the current SQLAlchemy schema to the database.

Requires DATABASE_URL in .env. Creates all tables that don't exist.
Does NOT alter existing tables (e.g. adding/renaming columns).
"""
import asyncio
import os
import sys

# Allow running from agents/ with agents package on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    from agents.db import create_tables

    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set. Set it in .env and try again.")
        sys.exit(1)
    await create_tables()
    print("Schema pushed: all tables created or already exist.")


if __name__ == "__main__":
    asyncio.run(main())
