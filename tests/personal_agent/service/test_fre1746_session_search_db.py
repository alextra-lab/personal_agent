"""Conversation search on real rows of the test Postgres (FRE-1746 AC-1, AC-2).

``SessionRepository.search`` must find a user's sessions by message text, of any age, newest first,
and must never return another user's session.

Marker: ``integration`` — excluded from ``make test`` because it needs a live DB.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from personal_agent.config import settings as app_settings
from personal_agent.service.repositories.session_repository import SessionRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_NOW = datetime.now(timezone.utc)


def _user(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant(content: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "model": "m",
        "model_role": "primary",
        "model_config_path": "c",
    }


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Return a session factory on the test Postgres, and refuse any other database."""
    assert ":5433/" in app_settings.database_url, "this test writes, so it runs on the test stack"
    engine = create_async_engine(app_settings.database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def users(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[tuple[UUID, UUID]]:
    """Seed two users. Delete them and their sessions afterwards."""
    owner, other = uuid4(), uuid4()
    async with maker() as db:
        for uid in (owner, other):
            await db.execute(
                text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
                {"uid": uid, "email": f"fre1746-{uid}@test.invalid"},
            )
        await db.commit()
    yield owner, other
    async with maker() as db:
        await db.execute(
            text("DELETE FROM sessions WHERE user_id IN (:a, :b)"), {"a": owner, "b": other}
        )
        await db.execute(
            text("DELETE FROM users WHERE user_id IN (:a, :b)"), {"a": owner, "b": other}
        )
        await db.commit()


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    user_id: UUID,
    messages: list[dict[str, Any]],
    *,
    age_days: int = 0,
) -> UUID:
    session_id = uuid4()
    when = _NOW - timedelta(days=age_days)
    async with maker() as db:
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, created_at, last_active_at, messages)"
                " VALUES (:sid, :uid, :ts, :ts, CAST(:messages AS jsonb))"
            ),
            {"sid": session_id, "uid": user_id, "ts": when, "messages": json.dumps(messages)},
        )
        await db.commit()
    return session_id


async def _search(
    maker: async_sessionmaker[AsyncSession], query: str, user_id: UUID, limit: int = 50
) -> list[UUID]:
    async with maker() as db:
        found = await SessionRepository(db).search(query, limit, user_id=user_id)
        return [s.session_id for s in found]


async def test_matches_user_and_assistant_text_ignoring_case(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """AC-1: the text of either role matches, in any letter case."""
    owner, _ = users
    by_user = await _seed(maker, owner, [_user("Tell me about Zanzibar spices")])
    by_assistant = await _seed(
        maker, owner, [_user("hello"), _assistant("The Zanzibar clove trade")]
    )
    await _seed(maker, owner, [_user("nothing relevant")])

    assert set(await _search(maker, "zanzibar", owner)) == {by_user, by_assistant}


async def test_returns_old_sessions_newest_first(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """AC-1: a session of any age is found, and the order is newest activity first."""
    owner, _ = users
    old = await _seed(maker, owner, [_user("quokka habitat")], age_days=400)
    newer = await _seed(maker, owner, [_user("quokka diet")], age_days=3)
    newest = await _seed(maker, owner, [_user("quokka photos")], age_days=0)

    assert await _search(maker, "quokka", owner) == [newest, newer, old]


async def test_never_returns_another_users_session(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """AC-2: a session of another user with the same word never appears, for either user."""
    owner, other = users
    mine = await _seed(maker, owner, [_user("pangolin facts")])
    theirs = await _seed(maker, other, [_user("pangolin secrets")])

    assert await _search(maker, "pangolin", owner) == [mine]
    assert await _search(maker, "pangolin", other) == [theirs]


async def test_like_wildcards_match_literally(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """A percent sign, an underscore and a backslash in the query match only themselves."""
    owner, _ = users
    percent = await _seed(maker, owner, [_user("a 100% sure thing")])
    underscore = await _seed(maker, owner, [_user("the snake_case rule")])
    backslash = await _seed(maker, owner, [_user(r"path C:\temp here")])
    await _seed(maker, owner, [_user("plain words only")])

    assert await _search(maker, "100%", owner) == [percent]
    assert await _search(maker, "%", owner) == [percent]
    assert await _search(maker, "e_c", owner) == [underscore]
    assert await _search(maker, "\\", owner) == [backslash]


async def test_json_keys_and_metadata_never_match(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """The search reads message text only, not role names or attribution fields."""
    owner, _ = users
    await _seed(maker, owner, [_user("hello"), _assistant("world")])

    assert await _search(maker, "model_role", owner) == []
    assert await _search(maker, "assistant", owner) == []
    assert await _search(maker, "primary", owner) == []


async def test_list_content_matches_text_blocks_only(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """A typed-block message matches on its text block, not on its image block."""
    owner, _ = users
    blocks = [
        {"type": "text", "text": "describe this axolotl"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,wombat"}},
    ]
    sid = await _seed(maker, owner, [_user(blocks)])  # type: ignore[arg-type]

    assert await _search(maker, "axolotl", owner) == [sid]
    assert await _search(maker, "wombat", owner) == []
    assert await _search(maker, "image_url", owner) == []


async def test_purged_and_malformed_sessions_do_not_match_or_raise(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """A purged session (empty list) and a non-array value are skipped without an error."""
    owner, _ = users
    await _seed(maker, owner, [])
    async with maker() as db:
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, created_at, last_active_at, messages)"
                " VALUES (:sid, :uid, :ts, :ts, 'null'::jsonb)"
            ),
            {"sid": uuid4(), "uid": owner, "ts": _NOW},
        )
        await db.commit()
    hit = await _seed(maker, owner, [_user("marmot burrow")])

    assert await _search(maker, "marmot", owner) == [hit]


async def test_limit_is_respected(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """The limit keeps the newest matches."""
    owner, _ = users
    ids = [await _seed(maker, owner, [_user(f"okapi {i}")], age_days=i) for i in range(5)]

    assert await _search(maker, "okapi", owner, limit=2) == ids[:2]


async def test_only_user_and_assistant_roles_match(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """Text that appears only in a tool or system message does not match."""
    owner, _ = users
    await _seed(
        maker,
        owner,
        [
            {"role": "system", "content": "ocelot system prompt"},
            {"role": "tool", "content": "ocelot tool output"},
        ],
    )

    assert await _search(maker, "ocelot", owner) == []


async def test_odd_json_shapes_are_skipped_without_an_error(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """Scalar elements, object or null content and non-string text values raise nothing."""
    owner, _ = users
    odd: list[Any] = [
        "bare string element",
        7,
        None,
        {"role": "user", "content": {"text": "tapir in an object"}},
        {"role": "user", "content": 42},
        {"role": "user", "content": None},
        {"role": "user"},
        {
            "role": "user",
            "content": [{"type": "text", "text": {"deep": "tapir in a nested object"}}],
        },
        {"role": "user", "content": ["tapir as a bare block", None, 3]},
    ]
    await _seed(maker, owner, odd)
    async with maker() as db:
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, created_at, last_active_at, messages)"
                ' VALUES (:sid, :uid, :ts, :ts, \'{"role": "user", "content": "tapir"}\'::jsonb)'
            ),
            {"sid": uuid4(), "uid": owner, "ts": _NOW},
        )
        await db.commit()
    hit = await _seed(maker, owner, [_user("tapir"), _user("tapir again")])

    assert await _search(maker, "tapir", owner) == [hit]


async def test_a_term_with_every_special_character_matches_only_itself(
    maker: async_sessionmaker[AsyncSession], users: tuple[UUID, UUID]
) -> None:
    """A mixed term escapes in the right order, and near misses do not match."""
    owner, _ = users
    exact = await _seed(maker, owner, [_user(r"see a\b%_c! here")])
    await _seed(maker, owner, [_user("see aXbYZc here")])
    await _seed(maker, owner, [_user(r"see a\\b%_c here")])

    assert await _search(maker, r"a\b%_c", owner) == [exact]
    assert await _search(maker, "!", owner) == [exact]
