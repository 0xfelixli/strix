"""Tests for the coverage manifest tool and the finish_scan recall gate."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from strix.tools.coverage import tools as cov
from strix.tools.finish.tool import _coverage_gate


@pytest.fixture
def fresh_manifest(tmp_path: Path) -> Iterator[Path]:
    """Point the module-global manifest at a fresh tmp file and reset after."""
    cov.hydrate_coverage_from_disk(tmp_path)
    yield tmp_path
    cov._coverage_storage.clear()
    cov._coverage_path = None


def test_add_units_dedupes_on_kind_and_location(fresh_manifest: Path) -> None:
    units = [
        {"kind": "sink", "location": "db.py:88", "title": "raw sql"},
        {"kind": "sink", "location": "db.py:88"},  # exact duplicate
        {"kind": "route", "location": "views.py:12"},
    ]
    res = cov._add_units_impl(json.dumps(units), "agent")
    assert res["success"]
    assert res["added"] == 2
    assert res["skipped_duplicates"] == 1
    assert res["summary"]["total"] == 2
    assert res["summary"]["pending"] == 2


def test_add_units_rejects_invalid_kind(fresh_manifest: Path) -> None:
    res = cov._add_units_impl(json.dumps([{"kind": "bogus", "location": "x"}]), "agent")
    assert not res["success"]
    assert "kind" in res["error"].lower()


def test_add_units_requires_location(fresh_manifest: Path) -> None:
    res = cov._add_units_impl(json.dumps([{"kind": "sink", "location": ""}]), "agent")
    assert not res["success"]


def test_mark_reviewed_requires_note_and_valid_status(fresh_manifest: Path) -> None:
    add = cov._add_units_impl(json.dumps([{"kind": "sink", "location": "a.py:1"}]), "agent")
    uid = add["unit_ids"][0]

    assert not cov._mark_reviewed_impl(uid, "reviewed", "")["success"]
    assert not cov._mark_reviewed_impl(uid, "bogus", "note")["success"]

    ok = cov._mark_reviewed_impl(uid, "reviewed", "checked, parameterized query")
    assert ok["success"]
    assert ok["updated"] == [uid]
    assert cov.pending_units() == []


def test_mark_reviewed_reports_missing_ids(fresh_manifest: Path) -> None:
    res = cov._mark_reviewed_impl("nope12", "ruled_out", "n/a")
    assert res["missing"] == ["nope12"]
    assert res["updated"] == []


def test_hydrate_roundtrip_persists_across_reload(tmp_path: Path) -> None:
    cov.hydrate_coverage_from_disk(tmp_path)
    cov._add_units_impl(json.dumps([{"kind": "file", "location": "a.py"}]), "seed")
    assert (tmp_path / "coverage.json").exists()

    # Simulate a fresh process / resume: clear memory, reload from disk.
    cov._coverage_storage.clear()
    cov.hydrate_coverage_from_disk(tmp_path)
    assert not cov.manifest_is_empty()
    assert cov._summary()["total"] == 1

    cov._coverage_storage.clear()
    cov._coverage_path = None


def test_gate_blocks_when_units_pending(fresh_manifest: Path) -> None:
    cov._add_units_impl(json.dumps([{"kind": "sink", "location": "a.py:1"}]), "agent")
    gate = _coverage_gate(parent_id=None)
    assert gate is not None
    assert gate["success"] is False
    assert gate["scan_completed"] is False
    assert gate["pending_count"] == 1


def test_gate_allows_when_all_dispositioned(fresh_manifest: Path) -> None:
    add = cov._add_units_impl(json.dumps([{"kind": "sink", "location": "a.py:1"}]), "agent")
    cov._mark_reviewed_impl(add["unit_ids"][0], "ruled_out", "not attacker-reachable")
    assert _coverage_gate(parent_id=None) is None


def test_gate_skips_subagents(fresh_manifest: Path) -> None:
    cov._add_units_impl(json.dumps([{"kind": "sink", "location": "a.py:1"}]), "agent")
    # A subagent (parent_id set) is never gated — only the root finishes the scan.
    assert _coverage_gate(parent_id="root-01") is None


def test_gate_allows_empty_manifest(fresh_manifest: Path) -> None:
    assert _coverage_gate(parent_id=None) is None


def test_gate_blocks_empty_manifest_for_whitebox(fresh_manifest: Path) -> None:
    gate = _coverage_gate(parent_id=None, is_whitebox=True)
    assert gate is not None
    assert gate["success"] is False
    assert gate["scan_completed"] is False
    assert gate["pending_count"] == 0
    assert "no attack-surface units" in gate["error"]


def test_gate_respects_disable_flag(fresh_manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cov._add_units_impl(json.dumps([{"kind": "sink", "location": "a.py:1"}]), "agent")

    class _Settings:
        class agents:  # noqa: N801
            disable_coverage_gate = True

    monkeypatch.setattr("strix.config.load_settings", lambda: _Settings)
    assert _coverage_gate(parent_id=None) is None


class _FakeExecResult:
    def __init__(self, stdout: bytes = b"", exit_code: int = 0, stderr: bytes = b"") -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = stderr


class _FakeSession:
    def __init__(self, result: _FakeExecResult) -> None:
        self._result = result

    async def exec(self, *_args: Any, **_kwargs: Any) -> _FakeExecResult:
        return self._result


async def test_seed_from_semgrep_uses_scanned_paths(fresh_manifest: Path) -> None:
    report = {"paths": {"scanned": ["a.py", "b.py", "a.py"]}, "results": []}
    session = _FakeSession(_FakeExecResult(stdout=json.dumps(report).encode()))
    res = await cov._seed_from_semgrep_impl({"sandbox_session": session}, "semgrep.json")
    assert res["success"]
    assert res["added"] == 2  # duplicate "a.py" path collapses to one unit


async def test_seed_from_semgrep_falls_back_to_results_paths(fresh_manifest: Path) -> None:
    report = {"paths": {}, "results": [{"path": "x.py"}, {"path": "x.py"}, {"path": "y.py"}]}
    session = _FakeSession(_FakeExecResult(stdout=json.dumps(report).encode()))
    res = await cov._seed_from_semgrep_impl({"sandbox_session": session}, "semgrep.json")
    assert res["success"]
    assert res["added"] == 2


async def test_seed_from_semgrep_handles_missing_session(fresh_manifest: Path) -> None:
    res = await cov._seed_from_semgrep_impl({}, "semgrep.json")
    assert not res["success"]
