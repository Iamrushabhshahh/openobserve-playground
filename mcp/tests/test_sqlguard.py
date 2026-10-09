from __future__ import annotations

import pytest

from agent_obs_mcp.sqlguard import SqlGuardError, guard_sql, strip_content


def allowed(name: str) -> bool:
    return name in ("claude_code", "codex")


def guard(sql: str, allow_content: bool = False, limit: int = 200):
    return guard_sql(sql, allowed, allow_content, limit)


def test_plain_select_gets_limit() -> None:
    g = guard('SELECT model, COUNT(*) FROM "claude_code" GROUP BY model')
    assert g.sql.endswith("LIMIT 200")
    assert g.streams == ("claude_code",)


def test_existing_limit_is_clamped_to_500() -> None:
    g = guard("SELECT * FROM claude_code LIMIT 10000", limit=10_000)
    assert g.sql == "SELECT * FROM claude_code LIMIT 500"
    assert g.limit == 500


def test_smaller_existing_limit_is_kept() -> None:
    assert guard("select a from claude_code limit 50").sql.endswith("LIMIT 50")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE claude_code",
        "SELECT 1 FROM claude_code; DROP TABLE claude_code",
        "SELECT * FROM claude_code;",
        "SELECT * INTO x FROM claude_code",
        "SELECT * FROM claude_code WHERE 1=1 -- hidden",
        "SELECT /* x */ 1 FROM claude_code",
        "WITH x AS (SELECT 1) DELETE FROM claude_code",
        "SELECT a FROM claude_code UNION SELECT b FROM users",
        "SELECT a FROM claude_code c JOIN secrets s ON c.a = s.a",
        'SELECT a FROM "other_stream"',
        "SELECT 1",
        "",
    ],
)
def test_rejected(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        guard(sql)


def test_allowed_streams_and_subqueries() -> None:
    g = guard(
        "SELECT session_id FROM (SELECT session_id FROM claude_code) r "
        "JOIN codex s ON r.session_id = s.session_id"
    )
    assert set(g.streams) == {"claude_code", "codex"}


def test_keywords_inside_string_literals_are_fine() -> None:
    g = guard("SELECT * FROM claude_code WHERE tool_name = 'drop; delete -- x'")
    assert "'drop; delete -- x'" in g.sql


def test_extract_from_is_not_mistaken_for_a_stream() -> None:
    guard("SELECT EXTRACT(HOUR FROM _timestamp) AS h FROM claude_code")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT user_prompt FROM claude_code",
        'SELECT "tool_input" FROM claude_code',
        "SELECT LENGTH(full_command) FROM claude_code",
        "SELECT session_id FROM claude_code WHERE response IS NOT NULL",
    ],
)
def test_content_columns_blocked_by_default(sql: str) -> None:
    with pytest.raises(SqlGuardError, match="AGENT_OBS_ALLOW_CONTENT"):
        guard(sql)


def test_content_columns_allowed_when_enabled() -> None:
    assert guard("SELECT user_prompt FROM claude_code", allow_content=True)


def test_strip_content_removes_content_keys() -> None:
    rows = [{"model": "m", "user_prompt": "secret", "system_prompt": "x", "tool_input": "{}"}]
    assert strip_content(rows) == [{"model": "m"}]
