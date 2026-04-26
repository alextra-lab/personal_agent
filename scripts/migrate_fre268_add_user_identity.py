#!/usr/bin/env python3
"""One-time migration: add users table + user_id FK on sessions (FRE-268).

Run once against an existing database before deploying the FRE-268 build.

Steps:
  1. Create the users table.
  2. Add a nullable user_id column to sessions.
  3. Insert the deployment owner row (from AGENT_OWNER_EMAIL env var or prompt).
  4. Backfill all existing sessions to that owner UUID.
  5. Make user_id NOT NULL.

Usage:
    uv run python scripts/migrate_fre268_add_user_identity.py

The script is idempotent — safe to run more than once.
"""

import asyncio
import sys
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from personal_agent.config.settings import get_settings

settings = get_settings()


async def run_migration() -> None:  # noqa: PLR0912
    """Execute the migration steps."""
    engine = create_async_engine(settings.database_url, echo=False)

    owner_email = settings.agent_owner_email
    if not owner_email:
        print("AGENT_OWNER_EMAIL is not set in .env.")
        owner_email = input("Enter the deployment owner's email: ").strip()
        if not owner_email:
            print("No owner email provided — aborting.")
            sys.exit(1)

    async with engine.begin() as conn:
        # Step 1: Create users table if not exists
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email       TEXT UNIQUE NOT NULL,
                display_name TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """))
        print("✓ users table ready")

        # Step 2: Add nullable user_id column to sessions if not exists
        col_exists = await conn.execute(text("""
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'sessions' AND column_name = 'user_id'
        """))
        if not col_exists.scalar_one_or_none():
            await conn.execute(text("""
                ALTER TABLE sessions ADD COLUMN user_id UUID REFERENCES users(user_id)
            """))
            print("✓ sessions.user_id column added (nullable)")
        else:
            print("✓ sessions.user_id column already exists")

        # Step 3: Upsert deployment owner into users table
        result = await conn.execute(
            text("""
                INSERT INTO users (email, created_at)
                VALUES (:email, now())
                ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                RETURNING user_id
            """),
            {"email": owner_email.lower()},
        )
        owner_user_id: UUID = result.scalar_one()
        print(f"✓ Owner user_id: {owner_user_id} ({owner_email})")

        # Step 4: Backfill existing sessions
        backfill = await conn.execute(
            text("""
                UPDATE sessions
                   SET user_id = :uid
                 WHERE user_id IS NULL
            """),
            {"uid": owner_user_id},
        )
        print(f"✓ Backfilled {backfill.rowcount} session(s) to owner")

        # Step 5: Make NOT NULL now that every row has a value
        await conn.execute(text("""
            ALTER TABLE sessions
              ALTER COLUMN user_id SET NOT NULL
        """))
        # Idempotent: if already NOT NULL, Postgres no-ops this
        print("✓ sessions.user_id is NOT NULL")

        # Step 6: Add index for fast per-user session listing
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_sessions_user_id
                ON sessions (user_id)
        """))
        print("✓ ix_sessions_user_id index ready")

    await engine.dispose()
    print("\nMigration complete.")
    print(f"Set AGENT_OWNER_EMAIL={owner_email} in your .env if not already set.")


if __name__ == "__main__":
    asyncio.run(run_migration())
