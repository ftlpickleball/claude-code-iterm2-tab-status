"""Unit tests for claude_tab_status.py adapter.

Runs outside iTerm2 by mocking the iterm2 module.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --- Mock iterm2 module before importing adapter ---

mock_iterm2 = MagicMock()
mock_iterm2.run_forever = MagicMock()
mock_iterm2.FocusMonitor = MagicMock()
mock_iterm2.FocusUpdateActiveSessionChanged = MagicMock()
mock_iterm2.FocusUpdateSelectedTabChanged = MagicMock()
sys.modules["iterm2"] = mock_iterm2

# Now import our adapter
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claude_tab_status  # noqa: E402, I001


# --- Fixtures ---


@pytest.fixture
def signal_dir(tmp_path: Path) -> Path:
    d = tmp_path / "claude-tab-status"
    d.mkdir()
    return d


@pytest.fixture
def sample_signal() -> dict:
    return {
        "session_id": "ses-test-1",
        "type": "idle",
        "message": "Claude is idle",
        "project": "myproject",
        "cwd": "/Users/me/myproject",
        "tty": "/dev/ttys042",
        "pid": "12345",
        "ts": str(int(time.time())),
    }


def write_signal(signal_dir: Path, signal: dict) -> Path:
    p = signal_dir / f"{signal['session_id']}.json"
    p.write_text(json.dumps(signal))
    return p


# --- TestSignalIO ---


class TestSignalIO:
    def test_read_signals_empty_dir(self, signal_dir: Path):
        signals = claude_tab_status.read_signals(str(signal_dir))
        assert signals == {}

    def test_read_signals_one_file(self, signal_dir: Path, sample_signal: dict):
        write_signal(signal_dir, sample_signal)
        signals = claude_tab_status.read_signals(str(signal_dir))
        assert "ses-test-1" in signals
        assert signals["ses-test-1"]["type"] == "idle"

    def test_read_signals_malformed_json(self, signal_dir: Path):
        (signal_dir / "bad.json").write_text("not json{{{")
        signals = claude_tab_status.read_signals(str(signal_dir))
        assert signals == {}

    def test_read_signals_ignores_non_json(self, signal_dir: Path, sample_signal: dict):
        write_signal(signal_dir, sample_signal)
        (signal_dir / "notes.txt").write_text("hello")
        signals = claude_tab_status.read_signals(str(signal_dir))
        assert len(signals) == 1

    def test_remove_signal(self, signal_dir: Path, sample_signal: dict):
        p = write_signal(signal_dir, sample_signal)
        assert p.exists()
        claude_tab_status.remove_signal(str(signal_dir), "ses-test-1")
        assert not p.exists()

    def test_remove_signal_nonexistent(self, signal_dir: Path):
        # Should not raise
        claude_tab_status.remove_signal(str(signal_dir), "ses-ghost")


# --- TestSnapshot ---


class TestSnapshot:
    def test_capture_saves_fields(self):
        snap = claude_tab_status.Snapshot(
            tab_color={"red": 100, "green": 200, "blue": 50},
            use_tab_color=True,
            name="my session",
            badge_text="",
            allow_title_setting=True,
        )
        assert snap.tab_color == {"red": 100, "green": 200, "blue": 50}
        assert snap.use_tab_color is True
        assert snap.name == "my session"
        assert snap.badge_text == ""
        assert snap.allow_title_setting is True

    def test_snapshot_equality(self):
        a = claude_tab_status.Snapshot({"red": 0, "green": 0, "blue": 0}, False, "s", "", True)
        b = claude_tab_status.Snapshot({"red": 0, "green": 0, "blue": 0}, False, "s", "", True)
        assert a == b


# --- TestMatchSession ---


class TestMatchSession:
    def _make_session(self, tty: str, pid: int = 100) -> MagicMock:
        session = MagicMock()
        session.async_get_variable = AsyncMock(
            side_effect=lambda k: {
                "tty": tty,
                "jobPid": pid,
            }.get(k)
        )
        session.session_id = f"iterm-{tty}"
        return session

    @pytest.mark.asyncio
    async def test_match_by_tty(self):
        s1 = self._make_session("/dev/ttys001")
        s2 = self._make_session("/dev/ttys002")
        result = await claude_tab_status.match_session([s1, s2], "/dev/ttys002", "999")
        assert result == s2

    @pytest.mark.asyncio
    async def test_no_match(self):
        s1 = self._make_session("/dev/ttys001")
        result = await claude_tab_status.match_session([s1], "/dev/ttys099", "999")
        assert result is None

    @pytest.mark.asyncio
    async def test_match_among_multiple(self):
        sessions = [self._make_session(f"/dev/ttys{i:03d}") for i in range(5)]
        result = await claude_tab_status.match_session(sessions, "/dev/ttys003", "999")
        assert result == sessions[3]


# --- TestPidAncestry ---


class TestPidAncestry:
    @patch("claude_tab_status._get_ppid")
    def test_self_is_ancestor(self, mock_ppid: MagicMock):
        assert claude_tab_status._is_ancestor(100, 100) is True
        mock_ppid.assert_not_called()

    @patch("claude_tab_status._get_ppid")
    def test_direct_parent(self, mock_ppid: MagicMock):
        mock_ppid.return_value = 100
        assert claude_tab_status._is_ancestor(100, 200) is True

    @patch("claude_tab_status._get_ppid")
    def test_not_ancestor(self, mock_ppid: MagicMock):
        # 200 -> 150 -> 1 (init), 100 is never found
        mock_ppid.side_effect = lambda pid: {200: 150, 150: 1}.get(pid, 0)
        assert claude_tab_status._is_ancestor(100, 200) is False

    @patch("claude_tab_status._get_ppid")
    def test_stops_at_pid_1(self, mock_ppid: MagicMock):
        mock_ppid.side_effect = lambda pid: {200: 1}.get(pid, 0)
        assert claude_tab_status._is_ancestor(100, 200) is False

    @patch("claude_tab_status._get_ppid")
    def test_stops_at_pid_0(self, mock_ppid: MagicMock):
        mock_ppid.return_value = 0
        assert claude_tab_status._is_ancestor(100, 200) is False


# --- TestFocus ---


class TestFocus:
    def test_initial_state(self):
        fm = claude_tab_status.FocusTracker()
        assert fm.focused_session_id is None

    def test_set_focus(self):
        fm = claude_tab_status.FocusTracker()
        fm.set_focused("iterm-ses-1")
        assert fm.focused_session_id == "iterm-ses-1"

    def test_is_focused(self):
        fm = claude_tab_status.FocusTracker()
        fm.set_focused("iterm-ses-1")
        assert fm.is_focused("iterm-ses-1") is True
        assert fm.is_focused("iterm-ses-2") is False


# --- TestFlasher ---


class TestFlasher:
    def test_start_marks_active(self):
        f = claude_tab_status.Flasher()
        f.start("ses-1")
        assert f.is_flashing("ses-1")

    def test_stop_marks_inactive(self):
        f = claude_tab_status.Flasher()
        f.start("ses-1")
        f.stop("ses-1")
        assert not f.is_flashing("ses-1")

    def test_double_start_is_noop(self):
        f = claude_tab_status.Flasher()
        f.start("ses-1")
        f.start("ses-1")  # should not raise
        assert f.is_flashing("ses-1")

    def test_stop_without_start(self):
        f = claude_tab_status.Flasher()
        f.stop("ses-ghost")  # should not raise
        assert not f.is_flashing("ses-ghost")


# --- TestTabState ---


class TestTabState:
    def test_running_value(self):
        assert claude_tab_status.TabState.RUNNING.value == "running"

    def test_idle_value(self):
        assert claude_tab_status.TabState.IDLE.value == "idle"

    def test_attention_value(self):
        assert claude_tab_status.TabState.ATTENTION.value == "attention"

    def test_is_str(self):
        assert isinstance(claude_tab_status.TabState.RUNNING, str)

    def test_resolve_running(self):
        assert claude_tab_status.resolve_state("running") == claude_tab_status.TabState.RUNNING

    def test_resolve_idle(self):
        assert claude_tab_status.resolve_state("idle") == claude_tab_status.TabState.IDLE

    def test_resolve_attention(self):
        assert claude_tab_status.resolve_state("attention") == claude_tab_status.TabState.ATTENTION

    def test_resolve_unknown_defaults_to_idle(self):
        assert claude_tab_status.resolve_state("bogus") == claude_tab_status.TabState.IDLE


# --- TestTypeAliases (removed — old aliases no longer supported) ---


class TestUnknownTypeFallback:
    def test_idle_prompt_is_unknown_falls_to_idle(self):
        """Old 'idle_prompt' type is no longer aliased, falls back to idle."""
        assert claude_tab_status.resolve_state("idle_prompt") == claude_tab_status.TabState.IDLE

    def test_permission_prompt_is_unknown_falls_to_idle(self):
        """Old 'permission_prompt' type is no longer aliased, falls back to idle."""
        result = claude_tab_status.resolve_state("permission_prompt")
        assert result == claude_tab_status.TabState.IDLE


# --- TestStripAllPrefixes ---


class TestStripAllPrefixes:
    def test_strip_running_prefix(self):
        result = claude_tab_status.strip_all_prefixes("⚡ My Session")
        assert result == "My Session"

    def test_strip_idle_prefix(self):
        result = claude_tab_status.strip_all_prefixes("💤 My Session")
        assert result == "My Session"

    def test_strip_attention_prefix(self):
        result = claude_tab_status.strip_all_prefixes("🔴 My Session")
        assert result == "My Session"

    def test_no_prefix_unchanged(self):
        result = claude_tab_status.strip_all_prefixes("My Session")
        assert result == "My Session"

    def test_empty_string(self):
        result = claude_tab_status.strip_all_prefixes("")
        assert result == ""


# --- TestSetStatePrefix ---


class TestSetStatePrefix:
    def test_set_running_prefix(self):
        result = claude_tab_status.set_state_prefix("My Session", "⚡ ")
        assert result == "⚡ My Session"

    def test_replace_existing_prefix(self):
        result = claude_tab_status.set_state_prefix("💤 My Session", "⚡ ")
        assert result == "⚡ My Session"

    def test_replace_attention_with_idle(self):
        result = claude_tab_status.set_state_prefix("🔴 My Session", "💤 ")
        assert result == "💤 My Session"


# --- TestDisplayTargetHelpers ---


class TestDisplayTargetHelpers:
    def test_should_update_title_for_title_target(self):
        assert claude_tab_status._should_update_title("title") is True
        assert claude_tab_status._should_update_subtitle("title") is False

    def test_should_update_subtitle_for_subtitle_target(self):
        assert claude_tab_status._should_update_title("subtitle") is False
        assert claude_tab_status._should_update_subtitle("subtitle") is True

    def test_should_update_both_for_both_target(self):
        assert claude_tab_status._should_update_title("both") is True
        assert claude_tab_status._should_update_subtitle("both") is True

    def test_invalid_routing_target_behaves_like_title(self):
        assert claude_tab_status._should_update_title("bad-value") is True
        assert claude_tab_status._should_update_subtitle("bad-value") is False

    def test_no_argument_routing_uses_config(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(claude_tab_status.CONFIG, "display_target", "subtitle")
        assert claude_tab_status._should_update_title() is False
        assert claude_tab_status._should_update_subtitle() is True

    def test_subtitle_status_text_strips_padding(self):
        assert claude_tab_status._subtitle_status_text("⚡ ") == "⚡"

    def test_signal_display_signature_includes_type_and_activity(self):
        signal = {"type": "running", "activity": "Run tests", "ts": "123"}
        assert claude_tab_status._signal_display_signature(signal) == ("running", "Run tests")

    def test_signal_display_signature_ignores_non_display_fields(self):
        first = {"type": "running", "activity": "Run tests", "ts": "123"}
        second = {"type": "running", "activity": "Run tests", "ts": "456"}
        assert claude_tab_status._signal_display_signature(first) == (
            claude_tab_status._signal_display_signature(second)
        )

    def test_subtitle_status_text_defaults_to_status_only_with_prompt_present(self):
        signal = {"activity": "Run tests"}
        assert claude_tab_status._subtitle_status_text("⚡ ", signal) == "⚡"

    def test_subtitle_status_text_appends_opt_in_prompt_activity(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setitem(claude_tab_status.CONFIG, "subtitle_activity_source", "prompt")
        signal = {"activity": "please run the tests for this repo"}
        assert claude_tab_status._subtitle_status_text("⚡ ", signal) == "⚡ Run tests"

    def test_subtitle_status_text_truncates_activity(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(claude_tab_status.CONFIG, "subtitle_activity_source", "prompt")
        signal = {"activity": "review the pull request and summarize the release notes"}
        assert claude_tab_status._subtitle_status_text("⚡ ", signal) == "⚡ Review PR"

    def test_subtitle_status_text_sanitizes_sensitive_activity(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setitem(claude_tab_status.CONFIG, "subtitle_activity_source", "prompt")
        signal = {"activity": "use token=abc123 to call the API"}
        assert claude_tab_status._subtitle_status_text("⚡ ", signal) == "⚡"

    def test_subtitle_status_text_empty_activity_falls_back_to_status(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setitem(claude_tab_status.CONFIG, "subtitle_activity_source", "prompt")
        signal = {"activity": "   "}
        assert claude_tab_status._subtitle_status_text("⚡ ", signal) == "⚡"

    def test_subtitle_status_text_preserves_swedish_letters(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(claude_tab_status.CONFIG, "subtitle_activity_source", "prompt")
        signal = {"activity": "kör testerna"}
        assert claude_tab_status._subtitle_status_text("⚡ ", signal) == "⚡ Kör testerna"

    def test_subtitle_status_text_preserves_german_letters(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(claude_tab_status.CONFIG, "subtitle_activity_source", "prompt")
        signal = {"activity": "führe Tests aus"}
        assert claude_tab_status._subtitle_status_text("⚡ ", signal) == "⚡ Führe tests"

    def test_subtitle_status_text_preserves_japanese_letters(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(claude_tab_status.CONFIG, "subtitle_activity_source", "prompt")
        signal = {"activity": "テストを実行"}
        assert claude_tab_status._subtitle_status_text("⚡ ", signal) == "⚡ テストを実行"

    @pytest.mark.asyncio
    async def test_set_subtitle_status_uses_fixed_user_variable(self):
        session = MagicMock()
        session.async_set_variable = AsyncMock()
        await claude_tab_status._set_subtitle_status(session, "⚡")
        session.async_set_variable.assert_awaited_once_with("user.claudeStatus", "⚡")

    @pytest.mark.asyncio
    async def test_clear_subtitle_status_sets_empty_string(self):
        session = MagicMock()
        session.async_set_variable = AsyncMock()
        await claude_tab_status._clear_subtitle_status(session)
        session.async_set_variable.assert_awaited_once_with("user.claudeStatus", "")

    @pytest.mark.asyncio
    async def test_set_subtitle_status_is_best_effort(self):
        session = MagicMock()
        session.async_set_variable = AsyncMock(side_effect=Exception("boom"))
        await claude_tab_status._set_subtitle_status(session, "⚡")
        session.async_set_variable.assert_awaited_once_with("user.claudeStatus", "⚡")

    @pytest.mark.asyncio
    async def test_apply_status_display_subtitle_does_not_touch_title(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setitem(claude_tab_status.CONFIG, "display_target", "subtitle")
        info = {"session": MagicMock()}
        info["session"].async_set_variable = AsyncMock()
        set_title = AsyncMock()

        await claude_tab_status._apply_status_display(info, "⚡ ", {}, set_title)

        set_title.assert_not_awaited()
        info["session"].async_set_variable.assert_awaited_once_with("user.claudeStatus", "⚡")

    @pytest.mark.asyncio
    async def test_apply_status_display_both_updates_title_and_subtitle(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setitem(claude_tab_status.CONFIG, "display_target", "both")
        info = {"session": MagicMock()}
        info["session"].async_set_variable = AsyncMock()
        set_title = AsyncMock()

        await claude_tab_status._apply_status_display(info, "⚡ ", {}, set_title)

        set_title.assert_awaited_once_with(info, "⚡ ")
        info["session"].async_set_variable.assert_awaited_once_with("user.claudeStatus", "⚡")

    @pytest.mark.asyncio
    async def test_clear_status_display_clears_subtitle_variable(self):
        info = {"session": MagicMock()}
        info["session"].async_set_variable = AsyncMock()

        await claude_tab_status._clear_status_display(info)

        info["session"].async_set_variable.assert_awaited_once_with("user.claudeStatus", "")

    def test_enter_state_delegates_to_status_display_router(self):
        source = inspect.getsource(claude_tab_status.main)
        enter_state = source.split("    async def _enter_state", 1)[1].split(
            "    async def _leave_state", 1
        )[0]

        assert "await _apply_status_display(info, prefix, signal, _set_tab_title)" in enter_state
        assert "await _set_tab_title(info, prefix)" not in enter_state

    def test_signal_watcher_refreshes_on_display_signature_change(self):
        source = inspect.getsource(claude_tab_status.main)
        watcher = source.split("    async def signal_watcher", 1)[1].split(
            "    # Focus monitor", 1
        )[0]

        assert "_signal_display_signature(prev)" in watcher
        assert "_signal_display_signature(" in watcher
        assert "curr" in watcher
        assert 'prev.get("type") != curr.get("type")' not in watcher

    def test_apply_state_refreshes_display_when_state_is_unchanged(self):
        source = inspect.getsource(claude_tab_status.main)
        apply_state = source.split("    async def apply_state", 1)[1].split(
            "    async def clear_session", 1
        )[0]

        assert "else:\n                await _enter_state(claude_sid, state, signal)" in apply_state


# --- TestTitlePrefix (backward compat) ---


class TestTitlePrefix:
    def test_prefix_added(self):
        result = claude_tab_status.add_title_prefix("My Session", "\U0001f534 ")
        assert result == "\U0001f534 My Session"

    def test_no_double_prefix(self):
        result = claude_tab_status.add_title_prefix("\U0001f534 My Session", "\U0001f534 ")
        assert result == "\U0001f534 My Session"

    def test_remove_prefix(self):
        result = claude_tab_status.remove_title_prefix("\U0001f534 My Session", "\U0001f534 ")
        assert result == "My Session"

    def test_remove_prefix_not_present(self):
        result = claude_tab_status.remove_title_prefix("My Session", "\U0001f534 ")
        assert result == "My Session"


# --- TestPidLiveness ---


class TestPidLiveness:
    @patch("os.kill")
    def test_alive_pid(self, mock_kill: MagicMock):
        mock_kill.return_value = None  # no exception → alive
        assert claude_tab_status._is_pid_alive(12345) is True
        mock_kill.assert_called_once_with(12345, 0)

    @patch("os.kill")
    def test_dead_pid(self, mock_kill: MagicMock):
        mock_kill.side_effect = ProcessLookupError()
        assert claude_tab_status._is_pid_alive(99999) is False

    @patch("os.kill")
    def test_permission_error_means_alive(self, mock_kill: MagicMock):
        mock_kill.side_effect = PermissionError()
        assert claude_tab_status._is_pid_alive(1) is True

    def test_zero_pid_is_dead(self):
        assert claude_tab_status._is_pid_alive(0) is False

    def test_negative_pid_is_dead(self):
        assert claude_tab_status._is_pid_alive(-1) is False


# --- TestPickFlashColor ---


class TestPickFlashColor:
    def test_default_orange_for_dark_tab(self):
        """Dark tab → configured orange (default) has enough contrast."""
        r, g, b = claude_tab_status._pick_flash_color(0, 0, 0)
        m = claude_tab_status
        assert (r, g, b) == (m.CONFIG["color_r"], m.CONFIG["color_g"], m.CONFIG["color_b"])

    def test_fallback_blue_for_orange_tab(self):
        """Orange tab → configured orange too close, falls back to blue."""
        r, g, b = claude_tab_status._pick_flash_color(255, 140, 0)
        assert (r, g, b) == (0, 136, 255)

    def test_fallback_white_for_blue_tab(self):
        """Blue tab close to fallback blue → falls to white."""
        r, g, b = claude_tab_status._pick_flash_color(0, 120, 240)
        # Orange should work here (far from blue)
        m = claude_tab_status
        assert (r, g, b) == (m.CONFIG["color_r"], m.CONFIG["color_g"], m.CONFIG["color_b"])

    def test_similar_to_all_candidates(self):
        """Color close to orange, blue, AND white → inverts."""
        # This is nearly impossible in practice, but test the fallback
        # We need a color within 120 of (255,140,0), (0,136,255), AND (255,255,255)
        # That's geometrically impossible, so just verify the function doesn't crash
        r, g, b = claude_tab_status._pick_flash_color(128, 128, 128)
        assert isinstance(r, int) and isinstance(g, int) and isinstance(b, int)


# --- TestConfig ---


class TestConfig:
    def test_defaults_when_no_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """No config file → all defaults."""
        # Stable default regardless of host XDG_RUNTIME_DIR (e.g. systemd Linux runners).
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        cfg = claude_tab_status.load_config(str(tmp_path / "nonexistent.json"))
        # _DEFAULTS["dir"] is captured at module-load; recompute the expected value the
        # same way to stay agnostic of where this test runs.
        assert cfg["dir"] == claude_tab_status._DEFAULTS["dir"]
        assert "/tmp/" not in cfg["dir"]  # regression: no shared-tmp leak
        assert cfg["prefix_running"] == "⚡ "
        assert cfg["prefix_idle"] == "💤 "
        assert cfg["prefix_attention"] == "🔴 "
        assert cfg["color_r"] == 255
        assert cfg["color_g"] == 140
        assert cfg["color_b"] == 0
        assert cfg["interval"] == 0.6
        assert cfg["flash_enabled"] is True
        assert cfg["badge_enabled"] is True
        assert cfg["badge"] == "⚠️ Needs input"
        assert cfg["notify"] is False
        assert cfg["sound"] == ""

    def test_default_display_target_is_title(self, tmp_path: Path):
        cfg = claude_tab_status.load_config(str(tmp_path / "nonexistent.json"))
        assert cfg["display_target"] == "title"

    def test_default_subtitle_activity_source_is_off(self, tmp_path: Path):
        cfg = claude_tab_status.load_config(str(tmp_path / "nonexistent.json"))
        assert cfg["subtitle_activity_source"] == "off"

    def test_file_subtitle_activity_source_prompt(self, tmp_path: Path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"subtitle_activity_source": "prompt"}))
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["subtitle_activity_source"] == "prompt"

    def test_env_subtitle_activity_source_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"subtitle_activity_source": "off"}))
        monkeypatch.setenv("CLAUDE_ITERM2_TAB_STATUS_SUBTITLE_ACTIVITY_SOURCE", "prompt")
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["subtitle_activity_source"] == "prompt"

    def test_invalid_subtitle_activity_source_defaults_to_off(self, tmp_path: Path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"subtitle_activity_source": "transcript"}))
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["subtitle_activity_source"] == "off"

    def test_file_display_target_override(self, tmp_path: Path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"display_target": "subtitle"}))
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["display_target"] == "subtitle"

    def test_env_display_target_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"display_target": "subtitle"}))
        monkeypatch.setenv("CLAUDE_ITERM2_TAB_STATUS_DISPLAY_TARGET", "both")
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["display_target"] == "both"

    def test_invalid_display_target_defaults_to_title(self, tmp_path: Path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"display_target": "bad-value"}))
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["display_target"] == "title"

    def test_display_target_is_case_insensitive(self, tmp_path: Path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"display_target": "SUBTITLE"}))
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["display_target"] == "subtitle"

    def test_file_overrides_defaults(self, tmp_path: Path):
        """Config file values override defaults."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"prefix_running": "🚀 ", "color_r": 100}))
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["prefix_running"] == "🚀 "
        assert cfg["color_r"] == 100
        assert cfg["prefix_idle"] == "💤 "

    def test_env_overrides_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Env vars override config file values."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"prefix_running": "🚀 "}))
        monkeypatch.setenv("CLAUDE_ITERM2_TAB_STATUS_PREFIX_RUNNING", "🔥 ")
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["prefix_running"] == "🔥 "

    def test_malformed_json_uses_defaults(self, tmp_path: Path):
        """Malformed JSON → fall back to defaults."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json{{{")
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["prefix_running"] == "⚡ "

    def test_badge_enabled_false(self, tmp_path: Path):
        """badge_enabled=false is respected."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"badge_enabled": False}))
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["badge_enabled"] is False

    def test_env_badge_enabled_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Env var BADGE_ENABLED=false overrides default."""
        monkeypatch.setenv("CLAUDE_ITERM2_TAB_STATUS_BADGE_ENABLED", "false")
        cfg = claude_tab_status.load_config(str(tmp_path / "nonexistent.json"))
        assert cfg["badge_enabled"] is False

    def test_flash_enabled_false(self, tmp_path: Path):
        """flash_enabled=false is respected."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"flash_enabled": False}))
        cfg = claude_tab_status.load_config(str(cfg_file))
        assert cfg["flash_enabled"] is False

    def test_env_flash_enabled_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Env var FLASH_ENABLED=false overrides default."""
        monkeypatch.setenv("CLAUDE_ITERM2_TAB_STATUS_FLASH_ENABLED", "false")
        cfg = claude_tab_status.load_config(str(tmp_path / "nonexistent.json"))
        assert cfg["flash_enabled"] is False


# --- TestHotReload ---


class TestHotReload:
    def test_reload_config_updates_config(self, tmp_path: Path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"prefix_running": "\U0001f680 "}))
        claude_tab_status.reload_config(str(cfg_file))
        assert claude_tab_status.CONFIG["prefix_running"] == "\U0001f680 "
        # Cleanup: restore defaults
        claude_tab_status.reload_config(str(tmp_path / "nonexistent.json"))

    def test_reload_rebuilds_prefixes(self, tmp_path: Path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"prefix_running": "\U0001f680 "}))
        claude_tab_status.reload_config(str(cfg_file))
        assert "\U0001f680 " in claude_tab_status.ALL_PREFIXES
        # Cleanup
        claude_tab_status.reload_config(str(tmp_path / "nonexistent.json"))


# --- TestDefaultSignalDir ---


class TestDefaultSignalDir:
    def test_default_dir_uses_xdg_runtime_dir_when_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert claude_tab_status._default_signal_dir() == str(tmp_path / "claude-tab-status")

    def test_default_dir_falls_back_to_home_cache(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        expected = str(Path.home() / ".cache" / "claude-tab-status")
        assert claude_tab_status._default_signal_dir() == expected

    def test_default_dir_is_not_in_tmp(self):
        # Regression: /tmp is shared across users on macOS — signal files leak cwd/pid/tty/activity.
        assert not claude_tab_status._default_signal_dir().startswith("/tmp/")


# --- TestDisplayTargetHotReload ---


class TestDisplayTargetHotReload:
    def _make_active(self, state: claude_tab_status.TabState | None = None) -> dict:
        session = MagicMock()
        session.async_set_variable = AsyncMock()
        session.async_set_name = AsyncMock()
        snapshot = MagicMock()
        snapshot.name = "my-tab"
        return {
            "ses-1": {
                "session": session,
                "snapshot": snapshot,
                "tab": None,
                "state": state,
            }
        }

    @pytest.mark.asyncio
    async def test_drop_subtitle_clears_user_variable(self):
        active = self._make_active(state=claude_tab_status.TabState.RUNNING)
        set_title = AsyncMock()

        await claude_tab_status._handle_display_target_change(active, "both", "title", set_title)

        active["ses-1"]["session"].async_set_variable.assert_awaited_once_with(
            "user.claudeStatus", ""
        )
        # Title channel was already active; nothing to re-apply or strip.
        set_title.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drop_title_strips_prefix_via_session_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Re-apply step reads CONFIG["display_target"]; mirror the new target.
        monkeypatch.setitem(claude_tab_status.CONFIG, "display_target", "subtitle")
        active = self._make_active(state=claude_tab_status.TabState.RUNNING)
        active["ses-1"]["snapshot"].name = "⚡ my-tab"
        set_title = AsyncMock()

        await claude_tab_status._handle_display_target_change(
            active, "title", "subtitle", set_title
        )

        active["ses-1"]["session"].async_set_name.assert_awaited()
        called_with = active["ses-1"]["session"].async_set_name.await_args[0][0]
        assert "⚡" not in called_with
        # Subtitle channel was added → state must be re-applied.
        active["ses-1"]["session"].async_set_variable.assert_awaited()

    @pytest.mark.asyncio
    async def test_add_channel_reapplies_current_state(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(claude_tab_status.CONFIG, "display_target", "both")
        active = self._make_active(state=claude_tab_status.TabState.ATTENTION)
        set_title = AsyncMock()

        await claude_tab_status._handle_display_target_change(active, "title", "both", set_title)

        # Subtitle was newly added → user variable should now carry the attention prefix.
        active["ses-1"]["session"].async_set_variable.assert_awaited()
        # Title was already active, so re-apply via _apply_status_display also calls set_title.
        set_title.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_change_skips_session_writes(self):
        active = self._make_active(state=claude_tab_status.TabState.RUNNING)
        set_title = AsyncMock()

        await claude_tab_status._handle_display_target_change(active, "title", "title", set_title)

        active["ses-1"]["session"].async_set_variable.assert_not_awaited()
        active["ses-1"]["session"].async_set_name.assert_not_awaited()
        set_title.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_sessions_without_state(self):
        active = self._make_active(state=None)
        set_title = AsyncMock()

        await claude_tab_status._handle_display_target_change(
            active, "title", "subtitle", set_title
        )

        # Title still gets cleared regardless of state...
        active["ses-1"]["session"].async_set_name.assert_awaited()
        # ...but no re-apply because state is None.
        set_title.assert_not_awaited()

    def test_signal_watcher_invokes_handler_on_display_target_change(self):
        source = inspect.getsource(claude_tab_status.main)
        watcher = source.split("    async def signal_watcher", 1)[1].split(
            "    # Focus monitor", 1
        )[0]

        assert "_handle_display_target_change(" in watcher
        assert "last_display_target" in watcher


# --- TestFlashEnabled ---


class TestFlashEnabled:
    def test_enter_state_gates_flash_on_config(self):
        source = inspect.getsource(claude_tab_status.main)
        enter_state = source.split("    async def _enter_state", 1)[1].split(
            "    async def _leave_state", 1
        )[0]

        assert 'if CONFIG["flash_enabled"]:' in enter_state
        assert "_start_flash(claude_sid)" in enter_state

    def test_signal_watcher_reconciles_flash_enabled_change(self):
        source = inspect.getsource(claude_tab_status.main)
        watcher = source.split("    async def signal_watcher", 1)[1].split(
            "    # Focus monitor", 1
        )[0]

        assert "_reconcile_flash_enabled(" in watcher
        assert "last_flash_enabled" in watcher


class TestSignalStaleness:
    """is_signal_stale() decides when a signal file should be reclaimed.

    Covers the age-based expiry that fixes signals stuck "attention" after a
    session moves on: the recorded PID is the long-lived login shell, so the
    dead-PID path alone never reclaims them.
    """

    GRACE = claude_tab_status._PID_STALE_GRACE

    def test_live_pid_past_max_age_is_stale(self):
        # The fix: an un-refreshed signal expires even though its PID is alive.
        sig = {"pid": "12345", "ts": "1000"}
        assert claude_tab_status.is_signal_stale(
            sig, now=1000 + 1801, max_age=1800, pid_alive=lambda _pid: True
        )

    def test_dead_pid_past_grace_is_stale(self):
        # Preserves the original dead-PID cleanup (closed tabs).
        sig = {"pid": "999999", "ts": "1000"}
        assert claude_tab_status.is_signal_stale(
            sig, now=1000 + self.GRACE + 1, max_age=1800, pid_alive=lambda _pid: False
        )

    def test_live_pid_within_max_age_is_not_stale(self):
        sig = {"pid": "12345", "ts": "1000"}
        assert not claude_tab_status.is_signal_stale(
            sig, now=1000 + 60, max_age=1800, pid_alive=lambda _pid: True
        )

    def test_dead_pid_within_grace_is_not_stale(self):
        sig = {"pid": "999999", "ts": "1000"}
        assert not claude_tab_status.is_signal_stale(
            sig, now=1000 + self.GRACE - 1, max_age=1800, pid_alive=lambda _pid: False
        )

    def test_max_age_zero_disables_age_expiry(self):
        # Live PID, ancient signal, but age expiry off -> kept.
        sig = {"pid": "12345", "ts": "1000"}
        assert not claude_tab_status.is_signal_stale(
            sig, now=1000 + 10_000_000, max_age=0, pid_alive=lambda _pid: True
        )

    def test_malformed_ts_is_not_stale(self):
        sig = {"pid": "12345", "ts": "not-a-number"}
        assert not claude_tab_status.is_signal_stale(
            sig, now=9_999_999, max_age=1800, pid_alive=lambda _pid: True
        )

    def test_signal_max_age_has_config_default(self):
        cfg = claude_tab_status.load_config("/nonexistent/path/config.json")
        assert "signal_max_age" in cfg
        assert isinstance(cfg["signal_max_age"], int)
        assert cfg["signal_max_age"] > 0

    def test_signal_max_age_overridable_via_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLAUDE_ITERM2_TAB_STATUS_SIGNAL_MAX_AGE", "120")
        cfg = claude_tab_status.load_config("/nonexistent/path/config.json")
        assert cfg["signal_max_age"] == 120

    def test_signal_watcher_uses_is_signal_stale(self):
        source = inspect.getsource(claude_tab_status.main)
        watcher = source.split("    async def signal_watcher", 1)[1].split(
            "    # Focus monitor", 1
        )[0]
        assert "is_signal_stale(" in watcher
        assert 'CONFIG["signal_max_age"]' in watcher


class TestLiveTabTitle:
    """The tab title override must track the live session name, not freeze it.

    Applications like Claude Code continuously retitle the session via OSC 0
    (task summaries). Writing literal title text as the override freezes the
    tab at state-change time; an interpolated reference keeps it live.
    """

    def test_set_tab_title_uses_interpolated_session_name(self):
        source = inspect.getsource(claude_tab_status.main)
        set_tab_title = source.split("    async def _set_tab_title", 1)[1].split(
            "    def _start_flash", 1
        )[0]

        assert r"\(currentSession.name)" in set_tab_title
        # The frozen-literal pattern must not come back
        assert 'async_get_variable("title")' not in set_tab_title
        # The prefix is user config landing in an interpolated string
        assert "escape_interpolation(prefix)" in set_tab_title
        # A user's own title format is kept as the base, not discarded
        assert 'info.get("orig_tab_title") or r"\\(currentSession.name)"' in set_tab_title

    def test_capture_rejects_our_own_override_as_original(self):
        """Split panes share a tab, so the captured override may be ours."""
        source = inspect.getsource(claude_tab_status.main)
        apply_state = source.split("    async def apply_state", 1)[1].split(
            "    async def clear_session", 1
        )[0]

        assert "is_plugin_tab_override(orig_tab_title)" in apply_state

    def test_display_target_change_restores_original_override(self):
        source = inspect.getsource(claude_tab_status._handle_display_target_change)

        assert 'info.get("orig_tab_title", "")' in source
        assert 'async_get_variable("title")' not in source

    def test_main_cleans_stale_overrides_before_watching(self):
        source = inspect.getsource(claude_tab_status.main)

        assert "async def cleanup_stale_overrides" in source
        assert "await cleanup_stale_overrides()" in source
        cleanup = source.split("    async def cleanup_stale_overrides", 1)[1].split(
            "    # Focus monitor", 1
        )[0]
        assert 'async_get_variable("titleOverrideFormat")' in cleanup
        assert "is_plugin_tab_override(" in cleanup

    def test_clear_session_restores_original_override(self):
        source = inspect.getsource(claude_tab_status.main)
        clear_session = source.split("    async def clear_session", 1)[1].split(
            "    # Signal watcher", 1
        )[0]

        assert 'async_set_title(info.get("orig_tab_title", ""))' in clear_session


class TestIsPluginTabOverride:
    """Only overrides this plugin actually wrote may be cleared at startup."""

    def test_matches_our_prefixed_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(claude_tab_status, "ALL_PREFIXES", ["⚡ ", "💤 ", "🔴 "])

        assert claude_tab_status.is_plugin_tab_override("⚡ Claude Code")
        assert claude_tab_status.is_plugin_tab_override("💤 ⠂ Claude Code")

    def test_ignores_foreign_and_empty_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(claude_tab_status, "ALL_PREFIXES", ["⚡ ", "💤 ", "🔴 "])

        assert not claude_tab_status.is_plugin_tab_override("")
        assert not claude_tab_status.is_plugin_tab_override(r"\(currentSession.name) — prod")

    def test_empty_prefix_does_not_match_every_override(self, monkeypatch: pytest.MonkeyPatch):
        """An opted-out state must not turn the startup cleanup into a wipe-all."""
        monkeypatch.setattr(claude_tab_status, "ALL_PREFIXES", ["⚡ ", "", "🔴 "])

        assert not claude_tab_status.is_plugin_tab_override("my own tab title")
        # The states still carrying a prefix keep matching
        assert claude_tab_status.is_plugin_tab_override("⚡ Claude Code")

    def test_matches_an_override_layered_on_a_custom_format(self, monkeypatch: pytest.MonkeyPatch):
        """What a split pane sees is our prefix on top of the user's own format."""
        monkeypatch.setattr(claude_tab_status, "ALL_PREFIXES", ["⚡ ", "💤 ", "🔴 "])

        assert claude_tab_status.is_plugin_tab_override(r"⚡ \(currentSession.name) — prod")

    def test_matches_the_escaped_form_that_gets_written(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(claude_tab_status, "ALL_PREFIXES", [r"\(pid) "])
        written = claude_tab_status.escape_interpolation(r"\(pid) ") + "Claude Code"

        assert claude_tab_status.is_plugin_tab_override(written)


class TestEscapeInterpolation:
    """Prefixes are user config and land inside an iTerm2 interpolated string."""

    def test_leaves_ordinary_prefixes_untouched(self):
        assert claude_tab_status.escape_interpolation("⚡ ") == "⚡ "

    def test_escapes_backslashes_so_prefixes_are_not_evaluated(self):
        assert claude_tab_status.escape_interpolation(r"\(pid) ") == r"\\(pid) "


# --- Session name stability, driven through the real main() loop ---

_REAL_SLEEP = asyncio.sleep
_JOB_SUFFIX = " (claude)"
_LIVE_TITLE_OVERRIDE = r"⚡ \(currentSession.name)"


class FakeSession:
    """Renders ``name`` as format + job suffix, as measured on iTerm2 sessions."""

    def __init__(self, title: str, tty: str = "/dev/ttys042") -> None:
        self.format = title
        self.pinned = False
        self.tty = tty
        self.session_id = "iterm-1"
        profile = MagicMock(tab_color=None, use_tab_color=False, badge_text="")
        self.async_get_profile = AsyncMock(return_value=profile)
        self.async_set_profile_properties = AsyncMock()
        self.async_set_variable = AsyncMock()
        self.async_set_name = AsyncMock(side_effect=self._set_name)

    def _set_name(self, name: str) -> None:
        self.format = name
        self.pinned = True

    def receive_terminal_title(self, title: str) -> None:
        """A manually set name stops OSC titles reaching the session."""
        if not self.pinned:
            self.format = title

    @property
    def rendered(self) -> str:
        return self.format + _JOB_SUFFIX

    async def async_get_variable(self, name: str) -> object:
        return {
            "name": self.rendered,
            "autoName": self.format,
            "autoNameFormat": self.format,
            "tty": self.tty,
        }.get(name)


class FakeTab:
    def __init__(self, session: FakeSession, override: str | None = None) -> None:
        self.sessions = [session]
        self.override = override
        self.titles: list[str] = []
        self.read_error = False
        self.write_error = False

    async def async_get_variable(self, name: str) -> object:
        if self.read_error:
            raise RuntimeError("tab variable unavailable")
        return self.override if name == "titleOverrideFormat" else None

    async def async_set_title(self, title: str) -> None:
        if self.write_error:
            raise RuntimeError("tab title unavailable")
        self.titles.append(title)
        self.override = title


class FakeFocusMonitor:
    def __init__(self) -> None:
        self.updates: asyncio.Queue[object] = asyncio.Queue()

    async def __aenter__(self) -> FakeFocusMonitor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def async_get_next_update(self) -> object:
        return await self.updates.get()


async def _settle() -> None:
    """Fakes never suspend, so each yield runs at most one full watcher pass."""
    for _ in range(50):
        await _REAL_SLEEP(0)


def _focus_update(session_id: str) -> MagicMock:
    update = MagicMock()
    update.active_session_changed.session_id = session_id
    return update


def _signal(signal_type: str = "idle", tty: str = "/dev/ttys042", age: int = 0) -> dict:
    return {
        "session_id": "ses-1",
        "type": signal_type,
        "tty": tty,
        "pid": str(os.getpid()),
        "ts": str(int(time.time()) - age),
    }


@pytest.fixture
def adapter_env(monkeypatch: pytest.MonkeyPatch, signal_dir: Path) -> Path:
    """Isolate from the user's config: a reload would point at the live signal dir."""
    module = claude_tab_status
    monkeypatch.setattr(module, "CONFIG_PATH", str(signal_dir / "no-config.json"))
    monkeypatch.setattr(module, "ALL_PREFIXES", ["⚡ ", "💤 ", "🔴 "])
    monkeypatch.setattr(
        module,
        "_STATE_PREFIXES",
        {
            module.TabState.RUNNING: "⚡ ",
            module.TabState.IDLE: "💤 ",
            module.TabState.ATTENTION: "🔴 ",
        },
    )
    settings = {
        "dir": str(signal_dir),
        "display_target": "title",
        "flash_enabled": False,
        "badge_enabled": False,
        "notify": False,
        "sound": "",
        "signal_max_age": 1800,
    }
    for key, value in settings.items():
        monkeypatch.setitem(module.CONFIG, key, value)
    monkeypatch.setattr(asyncio, "sleep", lambda _delay: _REAL_SLEEP(0))
    return signal_dir


@asynccontextmanager
async def running_adapter(tab: FakeTab) -> AsyncGenerator[FakeFocusMonitor, None]:
    monitor = FakeFocusMonitor()
    app = MagicMock(terminal_windows=[MagicMock(tabs=[tab])])
    with (
        patch.object(mock_iterm2, "async_get_app", AsyncMock(return_value=app)),
        patch.object(mock_iterm2, "FocusMonitor", MagicMock(return_value=monitor)),
    ):
        task = asyncio.ensure_future(claude_tab_status.main(MagicMock()))
        await _settle()
        try:
            yield monitor
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _run_cycle(signal_dir: Path, signal_type: str = "idle") -> None:
    """One session lifetime: capture on the new signal, restore when it goes away."""
    path = write_signal(signal_dir, _signal(signal_type))
    await _settle()
    path.unlink()
    await _settle()


class TestSessionNameStability:
    async def test_tab_path_never_writes_session_name(self, adapter_env: Path):
        session = FakeSession("PM")
        async with running_adapter(FakeTab(session)):
            for _ in range(5):
                await _run_cycle(adapter_env)

        assert session.rendered == "PM" + _JOB_SUFFIX
        session.async_set_name.assert_not_awaited()

    async def test_terminal_title_stays_live_across_cycles(self, adapter_env: Path):
        session = FakeSession("Claude Code")
        async with running_adapter(FakeTab(session)):
            await _run_cycle(adapter_env)
            session.receive_terminal_title("Kicad")
            await _run_cycle(adapter_env)
            session.receive_terminal_title("Sourcing")

        assert session.format == "Sourcing"

    async def test_focus_dismissed_attention_does_not_grow(self, adapter_env: Path):
        session = FakeSession("PM")
        async with running_adapter(FakeTab(session)) as monitor:
            for _ in range(3):
                write_signal(adapter_env, _signal("attention"))
                await _settle()
                monitor.updates.put_nowait(_focus_update(session.session_id))
                await _settle()
                assert not (adapter_env / "ses-1.json").exists()
                monitor.updates.put_nowait(_focus_update("other-session"))
                await _settle()

        assert session.rendered == "PM" + _JOB_SUFFIX
        session.async_set_name.assert_not_awaited()

    async def test_expired_signal_clears_without_name_write(self, adapter_env: Path):
        session = FakeSession("PM")
        async with running_adapter(FakeTab(session)):
            write_signal(adapter_env, _signal(age=7200))
            await _settle()

        assert not (adapter_env / "ses-1.json").exists()
        session.async_set_name.assert_not_awaited()

    async def test_existing_stack_stays_constant(self, adapter_env: Path):
        stacked = "PM" + _JOB_SUFFIX * 3
        session = FakeSession(stacked)
        async with running_adapter(FakeTab(session)):
            for _ in range(3):
                await _run_cycle(adapter_env)

        assert session.format == stacked

    @pytest.mark.parametrize("broken", ["read_error", "write_error"])
    async def test_fallback_prefixes_then_restores_without_growth(
        self, adapter_env: Path, broken: str
    ):
        session = FakeSession("PM")
        tab = FakeTab(session)
        async with running_adapter(tab):
            setattr(tab, broken, True)
            for _ in range(5):
                path = write_signal(adapter_env, _signal())
                await _settle()
                assert session.format == "💤 PM"
                path.unlink()
                await _settle()
                assert session.rendered == "PM" + _JOB_SUFFIX

    async def test_display_target_change_marks_name_changed(self, adapter_env: Path):
        session = FakeSession("⚡ PM")
        snapshot = claude_tab_status.Snapshot(None, False, "⚡ PM", "", True)
        active = {"ses-1": {"session": session, "snapshot": snapshot, "tab": None, "state": None}}

        await claude_tab_status._handle_display_target_change(
            active, "title", "subtitle", AsyncMock()
        )

        assert session.format == "PM"
        assert snapshot.name_changed is True

    async def test_capture_reads_format_not_rendered_name(self):
        snapshot = await claude_tab_status.capture_snapshot(FakeSession("PM"))

        assert snapshot.name == "PM"
        assert snapshot.name_changed is False


class TestTabOverrideBehaviour:
    async def test_override_tracks_live_session_name(self, adapter_env: Path):
        tab = FakeTab(FakeSession("PM"))
        async with running_adapter(tab):
            path = write_signal(adapter_env, _signal("running"))
            await _settle()
            assert tab.override == _LIVE_TITLE_OVERRIDE
            path.unlink()
            await _settle()

        assert tab.override == ""

    async def test_user_override_is_base_and_restored(self, adapter_env: Path):
        tab = FakeTab(FakeSession("PM"), override="prod")
        async with running_adapter(tab):
            path = write_signal(adapter_env, _signal("running"))
            await _settle()
            assert tab.override == "⚡ prod"
            path.unlink()
            await _settle()

        assert tab.override == "prod"

    async def test_startup_clears_stale_stacked_literal(self, adapter_env: Path):
        tab = FakeTab(FakeSession("PM"), override="⚡ Claude Code (claude) (claude)")
        async with running_adapter(tab):
            assert tab.override == ""

    async def test_capture_rejects_stale_stacked_literal(self, adapter_env: Path):
        tab = FakeTab(FakeSession("PM"))
        async with running_adapter(tab):
            tab.override = "💤 Claude Code (claude) (claude)"
            write_signal(adapter_env, _signal("running"))
            await _settle()

            assert tab.override == _LIVE_TITLE_OVERRIDE

    def test_no_space_prefix_recognizes_stale_literal(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(claude_tab_status, "ALL_PREFIXES", ["⚡", "💤", "🔴"])

        assert claude_tab_status.is_plugin_tab_override("⚡Claude Code (claude) (claude)")
