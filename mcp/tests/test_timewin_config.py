from __future__ import annotations

import pytest

from agent_obs_mcp.config import ConfigError, load_settings
from agent_obs_mcp.timewin import WindowError, days_range, parse_window, window_range


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("24h", 86_400), ("7d", 604_800), (" 30D ", 2_592_000), ("2w", 1_209_600), ("90m", 5_400)],
)
def test_parse_window(text: str, seconds: int) -> None:
    assert parse_window(text) == seconds


@pytest.mark.parametrize("bad", ["", "7", "7y", "-1d", "0d", "400d", "1.5h", "d7"])
def test_parse_window_rejects(bad: str) -> None:
    with pytest.raises(WindowError):
        parse_window(bad)


def test_window_range_is_microseconds_ending_now() -> None:
    rng = window_range("1h", now_s=1000.0)
    assert rng.end_us == 1_000_000_000
    assert rng.start_us == rng.end_us - 3_600_000_000
    assert rng.seconds == 3600


def test_days_range_bounds() -> None:
    assert days_range(2, now_s=0).seconds == 172_800
    with pytest.raises(WindowError):
        days_range(0)


def test_settings_defaults_and_auth() -> None:
    s = load_settings({"O2_USER": "u", "O2_PASSWORD": "p"})
    assert (s.url, s.org, s.claude_stream, s.codex_stream) == (
        "http://localhost:5080",
        "default",
        "claude_code",
        "codex",
    )
    assert s.allow_content is False
    assert s.auth_headers() == {"Authorization": "Basic dTpw"}
    assert "dTpw" not in repr(s)


def test_token_wins_and_safe_url_hides_userinfo() -> None:
    s = load_settings({"O2_TOKEN": "abc", "O2_URL": "http://u:pw@host:5080/"})
    assert s.auth_headers() == {"Authorization": "Basic abc"}
    assert s.safe_url == "http://host:5080"


def test_settings_reject_injection_in_stream_name() -> None:
    with pytest.raises(ConfigError):
        load_settings({"CLAUDE_STREAM": 'x" OR 1=1'})
    with pytest.raises(ConfigError):
        load_settings({"AGENT_OBS_TZ": "Mars/Olympus"})
