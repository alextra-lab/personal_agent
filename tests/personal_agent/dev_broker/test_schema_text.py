# ruff: noqa: D102
"""Migration 0038 and its init.sql mirror (FRE-1683). Source-only: no database.

The live grant behaviour is proven in ``test_role_integration.py``. This file guards the SQL text:
the mirror cannot drift, and no grant can widen silently.
"""

from __future__ import annotations

import re

from personal_agent.config.config_guard import repo_root

_MIGRATION = "docker/postgres/migrations/0038_dev_broker.sql"
_TABLES = ("dev_job_tokens", "dev_job_spend", "dev_month_spend", "dev_job_calls")


def _migration() -> str:
    return (repo_root() / _MIGRATION).read_text()


def _init_sql() -> str:
    return (repo_root() / "docker/postgres/init.sql").read_text()


def _broker_grants(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("GRANT") and line.strip().endswith("TO seshat_dev_broker;")
    ]


class TestMigration:
    def test_is_a_transaction(self) -> None:
        text = _migration()
        assert "\nBEGIN;\n" in text
        assert text.rstrip().endswith("COMMIT;")

    def test_is_idempotent(self) -> None:
        text = _migration()
        assert "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'seshat_dev_broker')" in text
        for table in _TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table} (" in text

    def test_the_role_gets_no_default_privileges(self) -> None:
        """A later table, such as dev_jobs, must never be granted to the broker by default."""
        for text in (_migration(), _init_sql()):
            sql = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("--")
            )
            for statement in re.findall(r"ALTER DEFAULT PRIVILEGES[^;]*;", sql):
                assert "seshat_dev_broker" not in statement

    def test_the_grants_name_only_the_d4_4_tables(self) -> None:
        allowed = set(_TABLES) | {"budget_counters", "budget_reservations", "api_costs"}
        allowed |= {"budget_counters_id_seq", "api_costs_id_seq", "personal_agent", "public"}
        for grant in _broker_grants(_migration()):
            names = set(re.findall(r"\b[a-z_]+\b", grant.split(" ON ", 1)[1]))
            names -= {"database", "schema", "sequence", "to", "seshat_dev_broker"}
            assert names <= allowed, grant

    def test_the_role_cannot_insert_a_token(self) -> None:
        for grant in _broker_grants(_migration()):
            if "dev_job_tokens" in grant:
                assert "INSERT" not in grant
                assert "DELETE" not in grant

    def test_documents_the_admin_apply_and_the_password(self) -> None:
        text = _migration()
        assert "AGENT_DATABASE_ADMIN_URL" in text
        assert "ALTER ROLE seshat_dev_broker PASSWORD" in text


class TestInitSqlMirror:
    def test_the_grants_match_the_migration(self) -> None:
        assert _broker_grants(_init_sql()) == _broker_grants(_migration())

    def test_the_tables_match_the_migration(self) -> None:
        migration, init_sql = _migration(), _init_sql()
        for table in _TABLES:
            pattern = rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);"
            from_migration = re.search(pattern, migration, re.DOTALL)
            from_init = re.search(pattern, init_sql, re.DOTALL)
            assert from_migration and from_init, table
            assert from_init.group(0) == from_migration.group(0), table

    def test_the_grants_follow_every_table_they_name(self) -> None:
        text = _init_sql()
        first_grant = text.find("TO seshat_dev_broker;")
        for table in (*_TABLES, "budget_counters", "budget_reservations", "api_costs"):
            created = text.find(f"CREATE TABLE IF NOT EXISTS {table} (")
            assert -1 < created < first_grant, table

    def test_the_tables_precede_the_app_role_grant(self) -> None:
        """seshat_app's ALL TABLES grant must cover them on a fresh install."""
        text = _init_sql()
        app_grant = text.find("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public")
        for table in _TABLES:
            assert -1 < text.find(f"CREATE TABLE IF NOT EXISTS {table} (") < app_grant, table
