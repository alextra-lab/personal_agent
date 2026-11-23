"""Share links on real rows of the test Postgres (ADR-0153 Amendment 1 / FRE-1782).

Proves the ticket's AC-1 to AC-8 and the ADR's V-L2, V-L3, V-L6, V-L7 (schema and headers on the
gateway side), V-L8, V-L9, V-L11, V-L12 and V-L13 against the table that migration 0042 creates.

Identity goes through the real ``get_request_user``: ``gateway_auth_enabled`` is on and the
main-app verifier is a stub that maps a test JWT to an email. R2 is an in-memory fake, and the
export fetcher returns pinned bytes, so nothing reaches the production bucket or a CDN (FRE-375).

Marker: ``integration`` — excluded from ``make test`` because it needs a live DB. Run it with the
test stack up (``make test-infra-up``) and migration 0042 applied there.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Iterator, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from personal_agent.config import settings
from personal_agent.service import artifacts_router, auth, share_links_router
from personal_agent.service.cf_access_jwt import CFAccessClaims, CFAccessVerifierError
from personal_agent.service.database import get_db_session
from personal_agent.storage.artifact_export import LibAsset, SubstitutionMap, compute_sri
from personal_agent.storage.artifact_store import ArtifactStoreError
from tests._helpers.log_capture import capture_log_records

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ORIGIN = "https://artifacts.example.com"
_SHARE_BASE = "https://share.example.com"
_INTERNAL = "fre1782-internal-token"
_OWNER = "fre1782-owner-a@example.com"
_UNLISTED = "fre1782-user-b@example.com"
_CREATOR = "fre1782-user-c@example.com"
_KATEX = "lib/katex@0.18.2/katex.min.js"
_CHART = "lib/chartjs@4.4.7/chart.umd.js"
_KATEX_BYTES = b"window.katex={render:function(){return 'katex-output'}};"
_RESOLVE = "/internal/share-links/resolve"
_MARKER_IP = "203.0.113.77"


# --- fakes ---------------------------------------------------------------------------------


class FakeStore:
    """In-memory stand-in for the R2 store. Never reaches a bucket."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put_after_write = False

    async def get(self, r2_key: str, *, trace_id: str | None = None) -> bytes:
        if r2_key not in self.objects:
            raise ArtifactStoreError(f"missing {r2_key}")
        return self.objects[r2_key]

    async def put(
        self, *, r2_key: str, content: bytes, content_type: str, trace_id: str | None = None
    ) -> None:
        self.objects[r2_key] = content
        if self.fail_put_after_write:
            raise ArtifactStoreError("put failed after the write")

    async def delete(self, r2_key: str, *, trace_id: str | None = None) -> None:
        self.objects.pop(r2_key, None)

    def share_objects(self) -> list[str]:
        return [key for key in self.objects if key.startswith("share/")]


class FakeFetcher:
    """Serves the pinned KaTeX bytes. The chart.js asset returns bytes that fail its SRI."""

    async def fetch(self, url: str) -> bytes:
        if url.endswith(_KATEX):
            return _KATEX_BYTES
        if url.endswith(_CHART):
            return b"tampered"
        raise AssertionError(f"unexpected fetch {url}")


def _sub_map() -> SubstitutionMap:
    def asset(path: str, sri_of: bytes) -> LibAsset:
        return LibAsset(
            lib_path=path,
            name=path.split("/")[1],
            kind="script",
            loading="classic",
            public_cdn_url=None,
            sri=compute_sri(sri_of),
            cors_verified=False,
            fallback="inline",
        )

    return SubstitutionMap(
        origin=_ORIGIN,
        by_lib_path={_KATEX: asset(_KATEX, _KATEX_BYTES), _CHART: asset(_CHART, b"pinned")},
    )


class _Verifier:
    """Main-app verifier stub: ``jwt:<email>`` verifies to that email, anything else fails."""

    async def verify(self, token: str) -> CFAccessClaims:
        if not token.startswith("jwt:"):
            raise CFAccessVerifierError("bad")
        return CFAccessClaims(email=token.removeprefix("jwt:"), sub="s", aud="a", iss="i")


# --- fixtures --------------------------------------------------------------------------------


@dataclass
class World:
    """The fixture items of the ticket, by letter."""

    maker: async_sessionmaker[AsyncSession]
    store: FakeStore
    app: FastAPI
    users: dict[str, UUID] = field(default_factory=dict)
    items: dict[str, UUID] = field(default_factory=dict)


_HTML_X = (
    "<html><head>"
    f'<script src="{_ORIGIN}/{_KATEX}"></script>'
    "</head><body><span id=m></span><script>katex.render('x^2', m)</script></body></html>"
)
_HTML_R = (
    "<html><body><script>"
    f"var s=document.createElement('script');s.src='{_ORIGIN}/'+'lib/katex@0.18.2/katex.min.js';"
    "document.head.appendChild(s);</script></body></html>"
)
_HTML_L = (
    "<html><body><script>"
    f"var s=document.createElement('script');s.src='{_ORIGIN}/lib/katex@0.18.2/katex.min.js';"
    "document.head.appendChild(s);</script></body></html>"
)
_HTML_OTHER_PATH = (
    f'<html><body><img src="{_ORIGIN}/00000000-0000-0000-0000-000000000000"></body></html>'
)
_HTML_SRI = f'<html><head><script src="{_ORIGIN}/{_CHART}"></script></head></html>'

# letter -> (owner, type, content_type, pending, title, body)
_ITEMS: dict[str, tuple[str, str, str, bool, str | None, bytes]] = {
    "X": ("A", "artifact", "text/html; charset=utf-8", False, "Report X", _HTML_X.encode()),
    "P": ("A", "upload", "image/png", False, "photo", b"\x89PNG-fake"),
    "J": ("A", "upload", "image/jpeg", False, "photo", b"jpeg-fake"),
    "F": ("A", "upload", "application/pdf", False, "doc", b"%PDF-fake"),
    "S": ("A", "upload", "image/svg+xml", False, "drawing", b"<svg/>"),
    "N": ("A", "note", "text/markdown; charset=utf-8", False, "note", b"# n"),
    "K": ("A", "capture", "image/png", False, "capture", b"cap"),
    "U": ("A", "upload", "image/png", True, "pending", b""),
    "M": ("A", "artifact", "text/markdown; charset=utf-8", False, "md", b"# m"),
    "R": ("A", "artifact", "text/html; charset=utf-8", False, "runtime", _HTML_R.encode()),
    "Q": ("A", "artifact", "text/html; charset=utf-8", False, "sri", _HTML_SRI.encode()),
    "L": ("A", "artifact", "text/html; charset=utf-8", False, "literal", _HTML_L.encode()),
    "O": ("A", "artifact", "text/html; charset=utf-8", False, "other", _HTML_OTHER_PATH.encode()),
    "H": ("A", "artifact", "text/html", False, 'Q3 "x"; y\r\nZ', b"<html>plain</html>"),
    "Z": ("B", "artifact", "text/html; charset=utf-8", False, "Z", b"<html>z</html>"),
    "CX": ("C", "artifact", "text/html; charset=utf-8", False, "C item", b"<html>c</html>"),
}


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as db:
            present = (
                await db.execute(text("SELECT to_regclass('artifact_share_links')"))
            ).scalar_one()
    except Exception as exc:  # noqa: BLE001 - environment guard
        await engine.dispose()
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    if present is None:
        await engine.dispose()
        pytest.skip("artifact_share_links missing; apply migration 0042 to the test stack")

    store = FakeStore()
    for name, value in {
        "gateway_auth_enabled": True,
        "agent_owner_email": _OWNER,
        "share_link_creator_emails": [_CREATOR],
        "share_public_base_url": _SHARE_BASE,
        "artifact_resolve_internal_token": _INTERNAL,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(auth, "get_main_app_verifier", lambda: _Verifier())

    @asynccontextmanager
    async def _auth_db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    monkeypatch.setattr(auth, "_get_db_session", _auth_db)
    monkeypatch.setattr(share_links_router, "AsyncSessionLocal", maker)
    monkeypatch.setattr(share_links_router, "get_artifact_store", lambda: store)
    monkeypatch.setattr(share_links_router, "load_substitution_map", _sub_map)
    monkeypatch.setattr(share_links_router, "_build_asset_fetcher", lambda _m: FakeFetcher())

    app = FastAPI()
    app.include_router(share_links_router.router)
    app.include_router(artifacts_router.router)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db_session] = _db

    w = World(maker=maker, store=store, app=app)
    async with maker() as db:
        for letter, email in {"A": _OWNER, "B": _UNLISTED, "C": _CREATOR}.items():
            w.users[letter] = await auth.get_or_create_user_by_email(db, email)
        for letter, (owner, kind, ctype, pending, title, body) in _ITEMS.items():
            item_id = uuid4()
            key = f"{kind}/{w.users[owner]}/GLOBAL/{item_id}.bin"
            await db.execute(
                text(
                    "INSERT INTO artifacts (id, user_id, type, title, content_type, size_bytes,"
                    " r2_key, created_by, upload_pending) VALUES (:id, :u, :t, :title, :ct, :sz,"
                    " :k, 'user', :p)"
                ),
                {
                    "id": item_id,
                    "u": w.users[owner],
                    "t": kind,
                    "title": title,
                    "ct": ctype,
                    "sz": len(body),
                    "k": key,
                    "p": pending,
                },
            )
            store.objects[key] = body
            w.items[letter] = item_id
        await db.commit()
    try:
        yield w
    finally:
        # The app role cannot delete a link (migration 0042), so the cleanup runs as the admin.
        admin = create_async_engine(settings.database_admin_url, poolclass=NullPool)
        async with async_sessionmaker(admin)() as db:
            ids = list(w.users.values())
            await db.execute(
                text("DELETE FROM artifact_share_links WHERE created_by = ANY(:ids)"), {"ids": ids}
            )
            await db.execute(text("DELETE FROM artifacts WHERE user_id = ANY(:ids)"), {"ids": ids})
            await db.execute(text("DELETE FROM users WHERE user_id = ANY(:ids)"), {"ids": ids})
            await db.commit()
        await admin.dispose()
        await engine.dispose()


def _client(w: World) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test")


def _as(email: str) -> dict[str, str]:
    return {"cf-access-jwt-assertion": f"jwt:{email}"}


_A, _B, _C = _as(_OWNER), _as(_UNLISTED), _as(_CREATOR)
_WORKER = {"x-internal-token": _INTERNAL}


async def _create(
    c: httpx.AsyncClient, w: World, letter: str, who: dict[str, str] = _A, **body: Any
) -> httpx.Response:
    return await c.post(
        f"/api/v1/artifacts/{w.items[letter]}/share-links", headers=who, json=body or None
    )


def _token(resp: httpx.Response) -> str:
    url = resp.json()["url"]
    assert url.startswith(f"{_SHARE_BASE}/s/")
    return url.removeprefix(f"{_SHARE_BASE}/s/")


async def _rows(w: World, where: str = "TRUE", **params: Any) -> list[dict[str, Any]]:
    async with w.maker() as db:
        result = await db.execute(
            text(f"SELECT row_to_json(l)::text AS j FROM artifact_share_links l WHERE {where}"),
            params,
        )
        return [json.loads(r.j) for r in result]


async def _count_for(w: World, letter: str) -> int:
    return len(await _rows(w, "artifact_id = :a", a=w.items[letter]))


async def _resolve(c: httpx.AsyncClient, token: str, **headers: str) -> httpx.Response:
    return await c.post(_RESOLVE, headers={**_WORKER, **headers}, json={"token": token})


def _fresh_token() -> str:
    return base64.urlsafe_b64encode(uuid4().bytes * 2).rstrip(b"=").decode()


# --- AC-1 / V-L2: expiry ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "days"),
    [
        ({"expires_in_days": 1}, 1),
        ({"expires_in_days": 7}, 7),
        ({"expires_in_days": 30}, 30),
        ({}, 7),
        (None, 7),
    ],
)
async def test_the_three_expiry_choices_and_the_default_give_exact_days(
    world: World, body: dict[str, int] | None, days: int
) -> None:
    async with _client(world) as c:
        resp = await c.post(
            f"/api/v1/artifacts/{world.items['P']}/share-links",
            headers=_A,
            **({} if body is None else {"json": body}),
        )
    assert resp.status_code == 201, resp.text
    async with world.maker() as db:
        delta = (
            await db.execute(
                text("SELECT expires_at - created_at FROM artifact_share_links WHERE id = :i"),
                {"i": UUID(resp.json()["link_id"])},
            )
        ).scalar_one()
    assert delta.days == days and delta.seconds == 0 and delta.microseconds == 0


@pytest.mark.parametrize("value", [0, 2, 31, None, True, 7.0, "7", -1])
async def test_a_refused_expiry_returns_422_and_inserts_no_row(world: World, value: Any) -> None:
    async with _client(world) as c:
        resp = await _create(c, world, "P", expires_in_days=value)
        extra = await c.post(
            f"/api/v1/artifacts/{world.items['P']}/share-links",
            headers=_A,
            json={"expires_in_days": 7, "other": 1},
        )
    assert resp.status_code == 422
    assert extra.status_code == 422
    assert await _count_for(world, "P") == 0


async def test_a_creator_sending_a_body_that_is_not_json_gets_422_and_no_row(
    world: World,
) -> None:
    async with _client(world) as c:
        resp = await c.post(
            f"/api/v1/artifacts/{world.items['P']}/share-links",
            headers={**_A, "content-type": "application/json"},
            content=b"{not json",
        )
        as_null = await c.post(
            f"/api/v1/artifacts/{world.items['P']}/share-links",
            headers={**_A, "content-type": "application/json"},
            content=b"null",
        )
    assert resp.status_code == 422
    assert as_null.status_code == 201
    assert await _count_for(world, "P") == 1


@pytest.mark.parametrize("expiry", ["NULL", "now() + INTERVAL '31 days'", "now()"])
async def test_a_direct_insert_without_a_valid_expiry_fails(world: World, expiry: str) -> None:
    async with world.maker() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                text(
                    "INSERT INTO artifact_share_links (id, artifact_id, created_by, token_hash,"
                    f" expires_at) VALUES (:i, :a, :u, :h, {expiry})"
                ),
                {"i": uuid4(), "a": world.items["P"], "u": world.users["A"], "h": b"\x00" * 32},
            )
    async with world.maker() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                text(
                    "INSERT INTO artifact_share_links (id, artifact_id, created_by, token_hash,"
                    " expires_at) VALUES (:i, :a, :u, :h, now() + INTERVAL '1 day')"
                ),
                {"i": uuid4(), "a": world.items["P"], "u": world.users["A"], "h": b"\x00" * 31},
            )


# --- AC-2 / V-L9: only a link creator manages links ------------------------------------------


async def test_no_jwt_gets_401_on_all_three_endpoints_even_with_a_bad_body(world: World) -> None:
    async with _client(world) as c:
        create = await c.post(
            f"/api/v1/artifacts/{world.items['X']}/share-links", json={"expires_in_days": 99}
        )
        not_json = await c.post(
            f"/api/v1/artifacts/{world.items['X']}/share-links",
            headers={"content-type": "application/json"},
            content=b"{not json",
        )
        bad_jwt = await _create(c, world, "X", who={"cf-access-jwt-assertion": "forged"})
        listing = await c.get("/api/v1/share-links")
        revoke = await c.delete(f"/api/v1/share-links/{uuid4()}")
    assert [
        create.status_code,
        not_json.status_code,
        bad_jwt.status_code,
        listing.status_code,
        revoke.status_code,
    ] == [
        401,
        401,
        401,
        401,
        401,
    ]
    assert await _count_for(world, "X") == 0


async def test_an_unlisted_user_gets_403_everywhere_also_on_its_own_item(world: World) -> None:
    async with _client(world) as c:
        own = await _create(c, world, "Z", who=_B)
        bad_body = await c.post(
            f"/api/v1/artifacts/{world.items['Z']}/share-links",
            headers={**_B, "content-type": "application/json"},
            content=b"{not json",
        )
        others = await _create(c, world, "X", who=_B)
        listing = await c.get("/api/v1/share-links", headers=_B)
        revoke = await c.delete(f"/api/v1/share-links/{uuid4()}", headers=_B)
    assert [r.status_code for r in (own, bad_body, others, listing, revoke)] == [403] * 5
    assert await _count_for(world, "Z") == 0


async def test_a_listed_creator_manages_only_its_own_links(world: World) -> None:
    async with _client(world) as c:
        a_link = await _create(c, world, "X")
        c_link = await _create(c, world, "CX", who=_C)
        c_on_x = await _create(c, world, "X", who=_C)
        c_list = (await c.get("/api/v1/share-links", headers=_C)).json()["items"]
        a_list = (await c.get("/api/v1/share-links", headers=_A)).json()["items"]
        c_revokes_a = await c.delete(f"/api/v1/share-links/{a_link.json()['link_id']}", headers=_C)
        a_revokes_c = await c.delete(f"/api/v1/share-links/{c_link.json()['link_id']}", headers=_A)
        random_revoke = await c.delete(f"/api/v1/share-links/{uuid4()}", headers=_C)
    assert a_link.status_code == 201 and c_link.status_code == 201
    assert c_on_x.status_code == 404
    assert [i["link_id"] for i in c_list] == [c_link.json()["link_id"]]
    assert [i["link_id"] for i in a_list] == [a_link.json()["link_id"]]
    assert c_revokes_a.status_code == 404 and a_revokes_c.status_code == 404
    assert c_revokes_a.json() == random_revoke.json()
    assert all(r["revoked_at"] is None for r in await _rows(world))


async def test_the_owner_on_another_users_item_gets_the_unknown_id_404(world: World) -> None:
    async with _client(world) as c:
        on_z = await _create(c, world, "Z")
        unknown = await c.post(f"/api/v1/artifacts/{uuid4()}/share-links", headers=_A)
    assert on_z.status_code == unknown.status_code == 404
    assert on_z.json() == unknown.json()
    assert on_z.headers.get("content-length") == unknown.headers.get("content-length")
    assert await _count_for(world, "Z") == 0


@pytest.mark.parametrize("letter", ["N", "K", "U", "M"])
async def test_an_item_that_is_not_shareable_returns_400_and_no_row(
    world: World, letter: str
) -> None:
    async with _client(world) as c:
        resp = await _create(c, world, letter)
    assert resp.status_code == 400
    assert await _count_for(world, letter) == 0


async def test_create_without_the_share_base_url_returns_503(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "share_public_base_url", None)
    async with _client(world) as c:
        resp = await _create(c, world, "X")
    assert resp.status_code == 503
    assert await _count_for(world, "X") == 0
    assert world.store.share_objects() == []


async def test_a_revoke_is_idempotent_and_keeps_the_first_time(world: World) -> None:
    async with _client(world) as c:
        link = (await _create(c, world, "P")).json()["link_id"]
        first = await c.delete(f"/api/v1/share-links/{link}", headers=_A)
        (row,) = await _rows(world, "id = :i", i=UUID(link))
        second = await c.delete(f"/api/v1/share-links/{link}", headers=_A)
        (again,) = await _rows(world, "id = :i", i=UUID(link))
    assert first.status_code == second.status_code == 204
    assert row["revoked_at"] is not None and again["revoked_at"] == row["revoked_at"]


async def test_can_share_link_is_true_only_for_a_creator_on_its_shareable_item(
    world: World,
) -> None:
    async with _client(world) as c:
        artifacts = (await c.get("/api/v1/artifacts?type=artifact", headers=_A)).json()["items"]
        uploads = (await c.get("/api/v1/artifacts?type=upload", headers=_A)).json()["items"]
        notes = (await c.get("/api/v1/artifacts?type=note", headers=_A)).json()["items"]
        x_meta = (await c.get(f"/api/v1/artifacts/{world.items['X']}", headers=_A)).json()
        m_meta = (await c.get(f"/api/v1/artifacts/{world.items['M']}", headers=_A)).json()
        b_list = (await c.get("/api/v1/artifacts?type=artifact", headers=_B)).json()["items"]
        z_meta = (await c.get(f"/api/v1/artifacts/{world.items['Z']}", headers=_B)).json()
        c_meta = (await c.get(f"/api/v1/artifacts/{world.items['CX']}", headers=_C)).json()
    flags = {i["artifact_id"]: i["can_share_link"] for i in artifacts + uploads + notes}
    by_letter = {k: flags.get(str(v)) for k, v in world.items.items()}
    assert by_letter["X"] is True and x_meta["can_share_link"] is True
    assert by_letter["P"] is True and by_letter["F"] is True and by_letter["S"] is True
    assert by_letter["N"] is False and by_letter["M"] is False and m_meta["can_share_link"] is False
    assert [i["can_share_link"] for i in b_list] == [False]
    assert z_meta["can_share_link"] is False
    assert c_meta["can_share_link"] is True


async def test_can_share_link_is_false_while_share_links_are_not_configured(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "share_public_base_url", None)
    async with _client(world) as c:
        x_meta = (await c.get(f"/api/v1/artifacts/{world.items['X']}", headers=_A)).json()
    assert x_meta["can_share_link"] is False


# --- AC-3 / V-L3 / V-L6: resolve outcomes ----------------------------------------------------


async def test_resolve_gives_the_documented_outcomes(world: World) -> None:
    async with _client(world) as c:
        x = await _create(c, world, "X")
        p = await _create(c, world, "P")
        expired = await _create(c, world, "F")
        revoked = await _create(c, world, "J")
        async with world.maker() as db:
            await db.execute(
                text(
                    "UPDATE artifact_share_links SET created_at = now() - INTERVAL '10 days',"
                    " expires_at = now() - INTERVAL '9 days' WHERE id = :i"
                ),
                {"i": UUID(expired.json()["link_id"])},
            )
            await db.commit()
        await c.delete(f"/api/v1/share-links/{revoked.json()['link_id']}", headers=_A)

        x_open = await _resolve(c, _token(x))
        p_open = await _resolve(c, _token(p))
        expired_open = await _resolve(c, _token(expired))
        revoked_open = await _resolve(c, _token(revoked))
        unknown = await _resolve(c, _fresh_token())
        malformed = await _resolve(c, "not-a-token")
        oversized = await _resolve(c, "A" * 5000)
        wrong_type = await c.post(_RESOLVE, headers=_WORKER, json={"token": 5})
        not_json = await c.post(_RESOLVE, headers=_WORKER, content=b"{")
        no_internal = await c.post(_RESOLVE, json={"token": _token(x)})
        wrong_internal = await c.post(
            _RESOLVE, headers={"x-internal-token": "wrong"}, json={"token": _token(x)}
        )

    assert x_open.status_code == 200
    assert x_open.json()["r2_key"] == f"share/{world.users['A']}/{x.json()['link_id']}.html"
    assert x_open.json()["content_type"] == "text/html; charset=utf-8"
    assert x_open.headers["cache-control"] == "private, no-store"
    assert p_open.status_code == 200
    async with world.maker() as db:
        p_key = (
            await db.execute(
                text("SELECT r2_key FROM artifacts WHERE id = :i"), {"i": world.items["P"]}
            )
        ).scalar_one()
    assert p_open.json()["r2_key"] == p_key and p_open.json()["content_type"] == "image/png"

    assert expired_open.status_code == revoked_open.status_code == 410
    assert expired_open.content == revoked_open.content
    assert expired_open.headers["cache-control"] == "private, no-store"
    for resp in (unknown, malformed, oversized, wrong_type, not_json):
        assert resp.status_code == 404
        assert resp.content == unknown.content
    assert "A" * 50 not in oversized.text
    assert no_internal.status_code == wrong_internal.status_code == 401

    for body in (unknown.text, expired_open.text):
        for secret in (str(world.items["X"]), "Report X", _OWNER, str(world.users["A"])):
            assert secret not in body


async def test_a_revoke_followed_at_once_by_a_resolve_gives_410(world: World) -> None:
    async with _client(world) as c:
        x = await _create(c, world, "X")
        before = await _resolve(c, _token(x))
        revoke = await c.delete(f"/api/v1/share-links/{x.json()['link_id']}", headers=_A)
        after = await _resolve(c, _token(x))
    assert before.status_code == 200 and before.headers["cache-control"] == "private, no-store"
    assert revoke.status_code == 204
    assert after.status_code == 410


async def test_a_resolve_blocked_behind_an_uncommitted_revoke_sees_the_revoke(world: World) -> None:
    async with _client(world) as c:
        x = await _create(c, world, "X")
        link_id = UUID(x.json()["link_id"])
        dsn = settings.database_url.replace("postgresql+asyncpg", "postgresql")
        conn = await asyncpg.connect(dsn)
        try:
            tx = conn.transaction()
            await tx.start()
            await conn.execute(
                "UPDATE artifact_share_links SET revoked_at = now() WHERE id = $1", link_id
            )
            pending = asyncio.create_task(_resolve(c, _token(x)))
            await asyncio.sleep(0.5)
            assert not pending.done(), "the resolve did not wait for the row lock"
            await tx.commit()
            resp = await asyncio.wait_for(pending, timeout=10)
        finally:
            await conn.close()
    assert resp.status_code == 410
    (row,) = await _rows(world, "id = :i", i=link_id)
    assert row["view_count"] == 0


# --- AC-4 / V-L11: views ---------------------------------------------------------------------


async def test_views_count_only_resolved_opens(world: World) -> None:
    async with _client(world) as c:
        x = await _create(c, world, "X")
        gone = await _create(c, world, "P")
        await c.delete(f"/api/v1/share-links/{gone.json()['link_id']}", headers=_A)
        for _ in range(2):
            assert (await _resolve(c, _token(x))).status_code == 200
        async with world.maker() as db:
            third_start = (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()
        assert (await _resolve(c, _token(x))).status_code == 200
        (row,) = await _rows(world, "id = :i", i=UUID(x.json()["link_id"]))

        assert (await _resolve(c, _fresh_token())).status_code == 404
        assert (await _resolve(c, _token(gone))).status_code == 410
        (unchanged,) = await _rows(world, "id = :i", i=UUID(x.json()["link_id"]))
        (gone_row,) = await _rows(world, "id = :i", i=UUID(gone.json()["link_id"]))
        listing = (await c.get("/api/v1/share-links", headers=_A)).json()["items"]

    assert row["view_count"] == 3
    assert datetime.fromisoformat(row["last_viewed_at"]) >= third_start
    assert unchanged == row
    assert gone_row["view_count"] == 0 and gone_row["last_viewed_at"] is None
    (item,) = listing
    assert item["view_count"] == 3
    assert datetime.fromisoformat(item["last_viewed_at"]) == datetime.fromisoformat(
        row["last_viewed_at"]
    )
    assert item["artifact_title"] == "Report X"


async def test_an_expired_link_is_absent_from_the_list(world: World) -> None:
    async with _client(world) as c:
        link = (await _create(c, world, "P")).json()["link_id"]
        async with world.maker() as db:
            await db.execute(
                text(
                    "UPDATE artifact_share_links SET created_at = now() - INTERVAL '2 days',"
                    " expires_at = now() - INTERVAL '1 day' WHERE id = :i"
                ),
                {"i": UUID(link)},
            )
            await db.commit()
        listing = (await c.get("/api/v1/share-links", headers=_A)).json()["items"]
    assert listing == []


# --- AC-5 / V-L8: the token is never stored or logged ------------------------------------------


def _holds(records: list[MutableMapping[str, Any]], needle: str) -> bool:
    return any(needle in json.dumps(r, default=str) for r in records)


async def test_the_token_is_stored_only_as_its_hash_and_never_logged(world: World) -> None:
    with capture_log_records() as logs:
        async with _client(world) as c:
            created = await _create(c, world, "X")
            revoked = await _create(c, world, "P")
            await c.delete(f"/api/v1/share-links/{revoked.json()['link_id']}", headers=_A)
            unknown_token = _fresh_token()
            assert (await _resolve(c, _token(created))).status_code == 200
            assert (await _resolve(c, unknown_token)).status_code == 404
            assert (await _resolve(c, _token(revoked))).status_code == 410
            listing = await c.get("/api/v1/share-links", headers=_A)

    events = {r.get("event") for r in logs}
    assert {
        "share_link_created",
        "share_link_revoked",
        "share_link_opened",
        "share_link_refused",
    } <= (events)
    for token in (_token(created), _token(revoked), unknown_token):
        raw = base64.urlsafe_b64decode(token + "=")
        digest = hashlib.sha256(raw).digest()
        assert not _holds(logs, token)
        assert not _holds(logs, digest.hex())
        assert not _holds(logs, base64.b64encode(digest).decode())
        assert token not in listing.text
    for resp, letter in ((created, "X"), (revoked, "P")):
        (row,) = await _rows(world, "id = :i", i=UUID(resp.json()["link_id"]))
        raw = base64.urlsafe_b64decode(_token(resp) + "=")
        assert row["token_hash"] == "\\x" + hashlib.sha256(raw).hexdigest()
        assert _token(resp) not in json.dumps(row)
        assert str(world.items[letter]) == row["artifact_id"]
    assert "url" not in listing.text and _SHARE_BASE not in listing.text


# --- AC-6 / V-L7: no IP address --------------------------------------------------------------


async def test_the_table_has_no_address_or_client_column(world: World) -> None:
    async with world.maker() as db:
        columns = (
            await db.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns"
                    " WHERE table_name = 'artifact_share_links' AND table_schema = 'public'"
                )
            )
        ).all()
    assert len(columns) == 10
    for name, data_type in columns:
        assert data_type not in {"inet", "cidr"}
        assert not any(part in name.lower() for part in ("ip", "agent", "referer", "referrer"))


async def test_a_forwarded_address_is_stored_and_logged_nowhere(world: World) -> None:
    with capture_log_records() as logs:
        async with _client(world) as c:
            x = await _create(c, world, "X")
            headers = {"x-forwarded-for": _MARKER_IP, "cf-connecting-ip": _MARKER_IP}
            assert (await _resolve(c, _token(x), **headers)).status_code == 200
            assert (await _resolve(c, _fresh_token(), **headers)).status_code == 404
    assert not _holds(logs, _MARKER_IP)
    assert all(_MARKER_IP not in json.dumps(r) for r in await _rows(world))


# --- AC-7 / V-L13: a failed create leaves nothing --------------------------------------------


async def test_an_sri_failure_returns_502_and_leaves_nothing(world: World) -> None:
    async with _client(world) as c:
        resp = await _create(c, world, "Q")
    assert resp.status_code == 502
    assert await _count_for(world, "Q") == 0
    assert world.store.share_objects() == []


@pytest.mark.parametrize("letter", ["R", "O"])
async def test_a_snapshot_that_still_needs_the_artifact_host_returns_422(
    world: World, letter: str
) -> None:
    """R assembles the toolkit URL from parts at run time; O points at a non-/lib/ path."""
    async with _client(world) as c:
        resp = await _create(c, world, letter)
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "this artifact loads files from the artifact host that a link cannot reach"
    )
    assert await _count_for(world, letter) == 0
    assert world.store.share_objects() == []


async def test_a_literal_toolkit_url_in_script_moves_to_the_share_host(world: World) -> None:
    """Owner decision of 2026-11-22: the share hostname serves the toolkit at /lib/."""
    async with _client(world) as c:
        resp = await _create(c, world, "L")
    assert resp.status_code == 201, resp.text
    (key,) = world.store.share_objects()
    snapshot = world.store.objects[key].decode()
    assert f"{_SHARE_BASE}/lib/katex@0.18.2/katex.min.js" in snapshot
    assert "artifacts.example.com" not in snapshot


async def test_a_put_that_wrote_and_then_failed_leaves_nothing(world: World) -> None:
    world.store.fail_put_after_write = True
    async with _client(world) as c:
        resp = await _create(c, world, "X")
    assert resp.status_code == 502
    assert await _count_for(world, "X") == 0
    assert world.store.share_objects() == []


class _FailingSession:
    """Wraps a real session and fails the share-link INSERT, or the commit, on purpose."""

    def __init__(self, inner: AsyncSession, *, fail: str) -> None:
        self._inner = inner
        self._fail = fail

    async def execute(self, statement: Any, params: Any = None) -> Any:
        if self._fail == "insert" and "INSERT INTO artifact_share_links" in str(statement):
            raise IntegrityError("INSERT", params, Exception("injected insert failure"))
        if self._fail == "resolve" and "UPDATE artifact_share_links AS l" in str(statement):
            raise OperationalError("UPDATE", params, Exception("injected connection loss"))
        return await self._inner.execute(statement, params)

    async def commit(self) -> None:
        if self._fail == "commit":
            raise IntegrityError("COMMIT", None, Exception("injected commit failure"))
        await self._inner.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.parametrize("fail", ["insert", "commit"])
async def test_an_injected_insert_or_commit_failure_leaves_nothing(world: World, fail: str) -> None:
    async def _db() -> AsyncIterator[Any]:
        async with world.maker() as session:
            yield _FailingSession(session, fail=fail)

    world.app.dependency_overrides[get_db_session] = _db
    transport = httpx.ASGITransport(app=world.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _create(c, world, "X")
    assert resp.status_code == 503
    assert await _count_for(world, "X") == 0
    assert world.store.share_objects() == []


@pytest.mark.parametrize("fail", ["insert", "resolve"])
async def test_a_database_error_logs_no_token_hash(world: World, fail: str) -> None:
    """The error text of a failed statement holds its parameters, among them token_hash (L3)."""
    async with _client(world) as c:
        token = _token(await _create(c, world, "P"))
    digest = hashlib.sha256(base64.urlsafe_b64decode(token + "=")).digest()

    async def _db() -> AsyncIterator[Any]:
        async with world.maker() as session:
            yield _FailingSession(session, fail=fail)

    world.app.dependency_overrides[get_db_session] = _db
    with capture_log_records() as logs:
        async with _client(world) as c:
            resp = await (_resolve(c, token) if fail == "resolve" else _create(c, world, "X"))
    assert resp.status_code == 503
    assert any(
        r.get("event") in {"share_link_create_db_failed", "share_link_resolve_db_failed"}
        for r in logs
    )
    for needle in (token, digest.hex(), repr(digest)[2:40]):
        assert not _holds(logs, needle)
        assert needle not in resp.text


class _CommitThenFailSession(_FailingSession):
    """Commits for real, then reports a failure: the commit outcome is unknown to the caller."""

    async def commit(self) -> None:
        await self._inner.commit()
        raise IntegrityError("COMMIT", None, Exception("acknowledgement lost"))


async def test_a_commit_that_landed_but_reported_failure_keeps_the_object(world: World) -> None:
    async def _db() -> AsyncIterator[Any]:
        async with world.maker() as session:
            yield _CommitThenFailSession(session, fail="none")

    world.app.dependency_overrides[get_db_session] = _db
    transport = httpx.ASGITransport(app=world.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _create(c, world, "X")
    assert resp.status_code == 503
    (row,) = await _rows(world, "artifact_id = :a", a=world.items["X"])
    assert world.store.share_objects() == [row["snapshot_r2_key"]]


async def test_a_failed_recheck_after_a_commit_failure_keeps_the_object(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _unknown(_link_id: UUID) -> None:
        return None

    monkeypatch.setattr(share_links_router, "_link_row_committed", _unknown)

    async def _db() -> AsyncIterator[Any]:
        async with world.maker() as session:
            yield _FailingSession(session, fail="commit")

    world.app.dependency_overrides[get_db_session] = _db
    transport = httpx.ASGITransport(app=world.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _create(c, world, "X")
    assert resp.status_code == 503
    assert await _count_for(world, "X") == 0
    assert len(world.store.share_objects()) == 1


async def test_a_create_cancelled_during_the_put_leaves_nothing(world: World) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    real_put = world.store.put

    async def _slow_put(**kwargs: Any) -> None:
        await real_put(**kwargs)
        started.set()
        await release.wait()

    world.store.put = _slow_put  # type: ignore[method-assign]
    async with _client(world) as c:
        task = asyncio.create_task(_create(c, world, "X"))
        await asyncio.wait_for(started.wait(), timeout=10)
        assert len(world.store.share_objects()) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.sleep(0.1)
    assert world.store.share_objects() == []
    assert await _count_for(world, "X") == 0


async def test_a_successful_create_keeps_its_snapshot(world: World) -> None:
    async with _client(world) as c:
        resp = await _create(c, world, "X")
    assert resp.status_code == 201
    (key,) = world.store.share_objects()
    snapshot = world.store.objects[key].decode()
    assert "katex-output" in snapshot and _ORIGIN not in snapshot
    assert key == f"share/{world.users['A']}/{resp.json()['link_id']}.html"


# --- AC-8 / V-L12: filenames through the resolve -----------------------------------------------


async def test_the_resolve_filename_is_safe_for_a_hostile_title(world: World) -> None:
    async with _client(world) as c:
        resp = await _resolve(c, _token(await _create(c, world, "H")))
    body = resp.json()
    assert "\r" not in body["content_disposition"] and "\n" not in body["content_disposition"]
    message = Message()
    message["Content-Disposition"] = body["content_disposition"]
    assert message.get_param("filename", header="Content-Disposition") == "Q3xyZ.html"
    assert body["filename"] == "Q3xyZ.html"


# --- AC-5 / V-L8, persisted: the test Elasticsearch on :9201 -----------------------------------


@pytest.fixture
def es_index_prefix() -> Iterator[str]:
    yield f"agent-logs-test-fre1782-{uuid4().hex[:8]}"


async def test_no_token_reaches_the_test_elasticsearch(world: World, es_index_prefix: str) -> None:
    import logging

    from elasticsearch import AsyncElasticsearch

    from personal_agent.telemetry.es_handler import ElasticsearchHandler

    es_url = settings.elasticsearch_url
    assert ":9201" in es_url, "FRE-375: this test writes only to the test Elasticsearch"
    handler = ElasticsearchHandler(es_url=es_url, index_prefix=es_index_prefix)
    if not await handler.connect():
        pytest.skip("test-stack Elasticsearch unavailable; run `make test-infra-up`")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        async with _client(world) as c:
            created = await _create(c, world, "X")
            revoked = await _create(c, world, "P")
            await c.delete(f"/api/v1/share-links/{revoked.json()['link_id']}", headers=_A)
            unknown = _fresh_token()
            await _resolve(c, _token(created))
            await _resolve(c, unknown)
            await _resolve(c, _token(revoked))
        assert await handler.drain()
    finally:
        root.removeHandler(handler)
        await handler.disconnect()

    client = AsyncElasticsearch(es_url)
    try:
        await client.indices.refresh(index=f"{es_index_prefix}*")
        found = await client.search(index=f"{es_index_prefix}*", size=500, query={"match_all": {}})
        docs = [json.dumps(h["_source"]) for h in found["hits"]["hits"]]
        assert any("share_link_created" in d for d in docs), "no share-link event reached ES"
        for token in (_token(created), _token(revoked), unknown):
            digest = hashlib.sha256(base64.urlsafe_b64decode(token + "=")).hexdigest()
            assert not any(token in d or digest in d for d in docs)
    finally:
        indices = await client.indices.get(index=f"{es_index_prefix}*")
        for name in indices:
            await client.indices.delete(index=name)
        await client.close()
