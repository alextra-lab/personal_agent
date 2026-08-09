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

    def test_grants_public_and_sysgraph_select(self) -> None:
        text = _migration_text()
        assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro" in text
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
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;",
            "GRANT USAGE ON SCHEMA sysgraph TO grafana_ro;",
            "GRANT SELECT ON ALL TABLES IN SCHEMA sysgraph TO grafana_ro;",
        ]
        for grant in shared_grants:
            assert grant in init_text, f"init.sql missing: {grant!r}"
            assert grant in migration_text, f"migration missing: {grant!r}"

    def test_appears_after_all_public_table_creation(self) -> None:
        """`GRANT SELECT ON ALL TABLES IN SCHEMA public` only covers tables that already exist —
        placed before the last CREATE TABLE, a fresh install would silently miss one.
        """
        text = _init_sql_text()
        last_create_table = text.rfind("CREATE TABLE")
        grafana_ro_grant = text.find("GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro")
        assert grafana_ro_grant > last_create_table > -1
