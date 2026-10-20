"""FRE-1622 (ADR-0157 D2): the session's Seshat effort level over the session API (AC-3).

The level follows ADR-0121 §4's invariants: an explicit stored value, a user-scoped write with
404 on a mismatch, hydration with provenance, and a notice to the active socket.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.config import load_model_config
from personal_agent.gateway.app import create_gateway_router
from tests.personal_agent.gateway.test_session_api import (
    _ALL_PROVIDERS_UP,
    _AUTH_HEADERS,
    _CHECK_ALL_PROVIDERS,
    _SELECTION_GET,
    _SELECTION_GET_ALL,
    _SESSION_GET,
    _TEST_USER_ID,
    _build_app_with_db_factory,
    _make_session_model,
    _patched_user_resolver,
    _served_ids_patch,
)

_EMIT_EFFORT = "personal_agent.transport.agui.transport.emit_session_effort"


def _session(level: str) -> MagicMock:
    model = _make_session_model(session_id=str(uuid4()))
    model.seshat_effort = level
    return model


class TestPatchEffort:
    def test_a_patch_writes_the_level_scoped_to_the_user_and_notifies_the_socket(self) -> None:
        db = AsyncMock()
        model = _session("standard")

        async def _refresh(row: MagicMock) -> None:
            # What the database holds after the UPDATE.
            row.seshat_effort = db.execute.await_args.args[0].compile().params["seshat_effort"]

        db.refresh = AsyncMock(side_effect=_refresh)
        get_mock = AsyncMock(return_value=model)
        with ExitStack() as stack:
            stack.enter_context(_patched_user_resolver())
            stack.enter_context(patch(_SESSION_GET, get_mock))
            emit = stack.enter_context(patch(_EMIT_EFFORT, new_callable=AsyncMock))
            app = _build_app_with_db_factory(db)
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.patch(
                    f"/api/v1/sessions/{model.session_id}/effort",
                    json={"seshat_effort": "fast"},
                    headers=_AUTH_HEADERS,
                )

        assert resp.status_code == 200
        assert resp.json()["seshat_effort"] == "fast"
        assert resp.json()["effort_provenance"] == "server-hydrated"
        assert get_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID
        update = db.execute.await_args.args[0].compile()
        assert update.params["seshat_effort"] == "fast"
        assert _TEST_USER_ID in update.params.values()  # the write is scoped to the owner
        db.commit.assert_awaited()
        emit.assert_awaited_once_with(session_id=str(model.session_id), seshat_effort="fast")

    def test_a_patch_from_another_user_returns_404_and_writes_nothing(self) -> None:
        db = AsyncMock()
        with ExitStack() as stack:
            stack.enter_context(_patched_user_resolver())
            stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=None))
            emit = stack.enter_context(patch(_EMIT_EFFORT, new_callable=AsyncMock))
            app = _build_app_with_db_factory(db)
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.patch(
                    f"/api/v1/sessions/{uuid4()}/effort",
                    json={"seshat_effort": "deep"},
                    headers=_AUTH_HEADERS,
                )

        assert resp.status_code == 404
        db.execute.assert_not_awaited()
        db.commit.assert_not_awaited()
        emit.assert_not_awaited()

    def test_an_unknown_level_is_refused_before_any_read(self) -> None:
        db = AsyncMock()
        get_mock = AsyncMock()
        with ExitStack() as stack:
            stack.enter_context(_patched_user_resolver())
            stack.enter_context(patch(_SESSION_GET, get_mock))
            app = _build_app_with_db_factory(db)
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.patch(
                    f"/api/v1/sessions/{uuid4()}/effort",
                    json={"seshat_effort": "turbo"},
                    headers=_AUTH_HEADERS,
                )

        assert resp.status_code == 422
        get_mock.assert_not_awaited()
        db.commit.assert_not_awaited()


class TestHydration:
    def test_the_session_get_carries_the_stored_level(self) -> None:
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=0.0)))
        model = _session("deep")
        with ExitStack() as stack:
            stack.enter_context(_patched_user_resolver())
            stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=model))
            stack.enter_context(patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None))
            app = _build_app_with_db_factory(db)
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get(f"/api/v1/sessions/{model.session_id}", headers=_AUTH_HEADERS)

        assert resp.json()["seshat_effort"] == "deep"
        assert resp.json()["effort_provenance"] == "server-hydrated"

    def test_the_config_get_carries_the_stored_level(self) -> None:
        from tests.personal_agent.gateway.test_session_api import _ALL_PROVIDERS_UP

        db = AsyncMock()
        model = _session("fast")
        with ExitStack() as stack:
            stack.enter_context(_patched_user_resolver())
            stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=model))
            stack.enter_context(patch(_SELECTION_GET_ALL, new_callable=AsyncMock, return_value={}))
            stack.enter_context(
                patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_ALL_PROVIDERS_UP)
            )
            stack.enter_context(_served_ids_patch())
            app = _build_app_with_db_factory(db)
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get(
                    f"/api/v1/sessions/{model.session_id}/config", headers=_AUTH_HEADERS
                )

        assert resp.json()["seshat_effort"] == "fast"
        assert resp.json()["effort_provenance"] == "server-hydrated"

    def test_a_value_outside_the_levels_hydrates_as_the_default(self) -> None:
        """Invariant 2's guard: provenance never claims a stored value it could not read."""
        from personal_agent.gateway.session_api import _session_effort

        model = _make_session_model()  # a MagicMock attribute, not a level
        assert _session_effort(model) == ("standard", "default")


class TestConfigCarriesTheModes:
    """FRE-1623 (ADR-0157 D2, D5): the selector reads each level's mode from the config GETs.

    ``effort_levels`` names the mode that each level requests. ``modes`` on a candidate names the
    modes that the deployment declares. The PWA shows a level's mode when the selected deployment
    declares it, and "model default" when it does not.
    """

    _LEVELS = {"fast": "fast", "standard": None, "deep": "deep"}

    @staticmethod
    def _primary_candidates(payload: dict) -> list[dict]:
        return payload["roles"]["primary"]["candidates"]

    def _assert_modes(self, payload: dict) -> None:
        assert payload["effort_levels"] == self._LEVELS
        catalog = load_model_config().models
        candidates = self._primary_candidates(payload)
        assert candidates
        for view in candidates:
            assert view["modes"] == list(catalog[view["key"]].modes), view["key"]
        # The selector tells apart a model that declares `fast` from one that does not.
        declares = [v["key"] for v in candidates if "fast" in v["modes"]]
        lacks = [v["key"] for v in candidates if "fast" not in v["modes"]]
        assert declares and lacks

    def test_the_sessionless_config_names_the_level_modes_and_the_declared_modes(self) -> None:
        with (
            patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_ALL_PROVIDERS_UP),
            _served_ids_patch(),
        ):
            app = FastAPI()
            app.include_router(create_gateway_router())
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get("/api/v1/config", headers=_AUTH_HEADERS)

        assert resp.status_code == 200
        self._assert_modes(resp.json())

    def test_the_session_config_names_the_level_modes_and_the_declared_modes(self) -> None:
        db = AsyncMock()
        model = _session("standard")
        with ExitStack() as stack:
            stack.enter_context(_patched_user_resolver())
            stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=model))
            stack.enter_context(patch(_SELECTION_GET_ALL, new_callable=AsyncMock, return_value={}))
            stack.enter_context(
                patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_ALL_PROVIDERS_UP)
            )
            stack.enter_context(_served_ids_patch())
            app = _build_app_with_db_factory(db)
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get(
                    f"/api/v1/sessions/{model.session_id}/config", headers=_AUTH_HEADERS
                )

        assert resp.status_code == 200
        self._assert_modes(resp.json())
