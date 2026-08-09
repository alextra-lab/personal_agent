# ruff: noqa: D103
"""FRE-1203 part 2 — the grafana_ro migration and its init.sql mirror.

Source-only: parses the committed SQL text directly, no live Postgres dependency. The actual
grant behaviour (SELECT works, write verbs refused) is proven live in
tests/integration/test_fre1203_grafana_log_lines_pg_datasource_acceptance.py — this suite guards
the SQL text against regressions (a write verb creeping into the grant list, the mirror drifting
from the migration, the apply-as-superuser instructions disappearing).
"""

from __future__ import annotations

from personal_agent.config.config_guard import repo_root

_MIGRATION_PATH = "docker/postgres/migrations/0025_grafana_readonly_role.sql"
_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER TABLE")
_LEDGER_TABLES = (
    "api_costs",
    "route_traces",
    "budget_policies",
    "budget_counters",
    "budget_reservations",
)


def _migration_text() -> str:
    return (repo_root() / _MIGRATION_PATH).read_text()


def _init_sql_text() -> str:
    return (repo_root() / "docker/postgres/init.sql").read_text()


class TestMigrationFile:
    def test_next_free_migration_number_used(self) -> None:
        migrations_dir = repo_root() / "docker/postgres/migrations"
        numbers = sorted(int(p.name[:4]) for p in migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        assert numbers[-1] == 25, (
            f"expected 0025 to be the highest migration, got {numbers[-1]:04d}"
        )

    def test_is_idempotent(self) -> None:
        text = _migration_text()
        assert "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro')" in text

    def test_wrapped_in_a_transaction(self) -> None:
        text = _migration_text()
        assert text.strip().startswith("-- ") and "\nBEGIN;\n" in text
        assert text.rstrip().endswith("COMMIT;")

    def test_grants_select_only_never_a_write_verb(self) -> None:
        """The whole point of grafana_ro: SELECT-only containment against a Grafana Viewer that
        can issue arbitrary queries to every datasource (ADR-0129). A write verb anywhere in this
        file's GRANT statements would defeat that.
        """
        text = _migration_text()
        grant_lines = [
            line for line in text.splitlines() if line.strip().upper().startswith("GRANT")
        ]
        assert grant_lines, "expected at least one GRANT statement"
        for line in grant_lines:
            for verb in _WRITE_VERBS:
                assert verb not in line.upper(), f"write verb {verb!r} found in grant: {line!r}"

    def test_grants_only_the_named_ledger_tables_on_public(self) -> None:
        """Owner ruling 2026-08-09: table-grain, not `ALL TABLES IN SCHEMA public` — `public`
        also holds `users.email` (PII) and raw conversation content the ticket never asked to
        expose. Each of the five ledger tables must be named explicitly.
        """
        text = _migration_text()
        for table in _LEDGER_TABLES:
            assert f"public.{table}" in text, f"expected an explicit grant naming public.{table}"
        assert "GRANT SELECT ON ALL TABLES IN SCHEMA public" not in text

    def test_no_default_privileges_on_public(self) -> None:
        """The removed clause is the one that most needed removing: it silently grants every
        FUTURE public table — including any that later holds PII — with nobody deciding.
        """
        text = _migration_text()
        assert "ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public" not in text

    def test_grants_sysgraph_select_unaffected_by_the_narrowing(self) -> None:
        text = _migration_text()
        assert "GRANT SELECT ON ALL TABLES IN SCHEMA sysgraph TO grafana_ro" in text

    def test_sysgraph_default_privileges_target_the_schema_owner_not_agent(self) -> None:
        """Sysgraph objects are created under `SET ROLE sysgraph_role` (migration 0014's
        convention) — a default-privileges grant `FOR ROLE agent` would silently miss every
        future table created there.
        """
        text = _migration_text()
        assert (
            "ALTER DEFAULT PRIVILEGES FOR ROLE sysgraph_role IN SCHEMA sysgraph\n"
            "    GRANT SELECT ON TABLES TO grafana_ro;" in text
        )

    def test_documents_the_admin_url_apply_command(self) -> None:
        text = _migration_text()
        assert "AGENT_DATABASE_ADMIN_URL" in text
        assert "psql" in text


class TestInitSqlMirror:
    """Fresh installs only run init.sql (migrations bring existing DBs current) — the grant
    logic must be duplicated, not merely referenced.
    """

    def test_grafana_ro_role_present(self) -> None:
        text = _init_sql_text()
        assert "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro')" in text

    def test_grants_match_the_migration(self) -> None:
        init_text = _init_sql_text()
        migration_text = _migration_text()
        shared_grants = [
            "GRANT CONNECT ON DATABASE personal_agent TO grafana_ro;",
            "GRANT USAGE ON SCHEMA public TO grafana_ro;",
            "GRANT USAGE ON SCHEMA sysgraph TO grafana_ro;",
            "GRANT SELECT ON ALL TABLES IN SCHEMA sysgraph TO grafana_ro;",
        ]
        for grant in shared_grants:
            assert grant in init_text, f"init.sql missing: {grant!r}"
            assert grant in migration_text, f"migration missing: {grant!r}"
        for table in _LEDGER_TABLES:
            assert f"public.{table}" in init_text, f"init.sql missing grant on public.{table}"
            assert f"public.{table}" in migration_text, f"migration missing grant on public.{table}"
        # `ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public` legitimately exists for
        # seshat_app (pre-existing, unrelated) — the assertion is that grafana_ro is never its
        # target, not that the phrase never appears in the file at all. `sysgraph`'s own
        # default-privileges clause also legitimately ends in the same "GRANT SELECT ON TABLES
        # TO grafana_ro" text, so the check must be scoped to the `agent`+`public` combination
        # specifically, not that substring anywhere.
        forbidden = (
            "ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public\n"
            "    GRANT SELECT ON TABLES TO grafana_ro;"
        )
        assert forbidden not in init_text
        assert forbidden not in migration_text

    def test_appears_after_the_five_ledger_tables_are_created(self) -> None:
        """The explicit per-table grant only works if each named table already exists — placed
        before any of the five, a fresh install would fail with an undefined-table error.
        """
        text = _init_sql_text()
        grafana_ro_grant = text.find("public.api_costs,\n    public.route_traces")
        assert grafana_ro_grant > -1
        for table in _LEDGER_TABLES:
            create_stmt = text.find(f"CREATE TABLE IF NOT EXISTS {table}")
            assert -1 < create_stmt < grafana_ro_grant, (
                f"public.{table} must be created before the grafana_ro grant"
            )
