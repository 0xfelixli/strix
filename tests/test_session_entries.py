"""Tests for build_session_entries: splitting copied vs bind-mounted sources."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from agents.sandbox.entries import LocalDir

from strix.runtime import session_manager
from strix.runtime.session_manager import build_session_entries


if TYPE_CHECKING:
    from pathlib import Path


def _source(subdir: str, path: str, *, mount: bool = False) -> dict[str, Any]:
    return {"source_path": path, "workspace_subdir": subdir, "mount": mount}


def _force_isolate(monkeypatch: Any, *, enabled: bool) -> None:
    """Pin runtime.local_isolate so tests don't inherit the machine's config."""

    class _Runtime:
        local_isolate = enabled
        setup_cmd = None  # keeps _run_setup_command a no-op (no session.exec)
        setup_timeout = 600

    class _Settings:
        runtime = _Runtime()

    def _fake_load_settings() -> _Settings:
        return _Settings()

    monkeypatch.setattr(session_manager, "load_settings", _fake_load_settings)


def test_copied_source_becomes_localdir_entry(tmp_path: Path) -> None:
    entries, bind_mounts = build_session_entries([_source("repo", str(tmp_path))])

    assert bind_mounts == []
    assert isinstance(entries["repo"], LocalDir)
    assert entries["repo"].src == tmp_path.resolve()


def test_mounted_source_becomes_bind_mount(tmp_path: Path) -> None:
    entries, bind_mounts = build_session_entries([_source("repo", str(tmp_path), mount=True)])

    assert entries == {}
    assert bind_mounts == [
        {
            "source": str(tmp_path.resolve()),
            "target": "/workspace/repo",
            "read_only": True,
        }
    ]


def test_mixed_sources_split_correctly(tmp_path: Path) -> None:
    copied = tmp_path / "copied"
    mounted = tmp_path / "mounted"
    copied.mkdir()
    mounted.mkdir()

    entries, bind_mounts = build_session_entries(
        [
            _source("copied", str(copied)),
            _source("mounted", str(mounted), mount=True),
        ]
    )

    assert list(entries) == ["copied"]
    assert isinstance(entries["copied"], LocalDir)
    assert [m["target"] for m in bind_mounts] == ["/workspace/mounted"]


def test_incomplete_sources_are_skipped() -> None:
    entries, bind_mounts = build_session_entries(
        [
            {"source_path": "", "workspace_subdir": "x"},
            {"source_path": "/p", "workspace_subdir": ""},
        ]
    )
    assert entries == {}
    assert bind_mounts == []


async def test_create_local_zero_copy_bundle(tmp_path: Path, monkeypatch: Any) -> None:
    """Local backend (isolate off): manifest root = the real repo, no entries."""
    captured: dict[str, Any] = {}

    async def _fake_backend(*, image: str, manifest: Any, exposed_ports: Any, bind_mounts: Any):
        captured["manifest"] = manifest
        captured["exposed_ports"] = exposed_ports
        return object(), object()

    monkeypatch.setattr(session_manager, "get_backend", lambda _name: _fake_backend)
    # Hermetic: don't inherit STRIX_LOCAL_ISOLATE from the machine's config.
    _force_isolate(monkeypatch, enabled=False)

    bundle = await session_manager._create_local(
        "scan-x", local_sources=[_source("repo", str(tmp_path))]
    )

    resolved = str(tmp_path.resolve())
    assert bundle["workspace_root"] == resolved
    assert bundle["workspace_clone"] is None
    # Zero-copy: root points at the repo and no LocalDir entries are streamed.
    assert str(captured["manifest"].root) == resolved
    assert dict(captured["manifest"].entries) == {}
    assert captured["exposed_ports"] == ()


async def test_create_local_rejects_multiple_sources(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exactly one local source"):
        await session_manager._create_local(
            "scan-x",
            local_sources=[
                _source("a", str(tmp_path / "a")),
                _source("b", str(tmp_path / "b")),
            ],
        )


def test_cow_clone_copies_and_leaves_source_untouched(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "app.py").write_text("SECRET = 'x'\n", encoding="utf-8")
    dst = tmp_path / "run" / "workspace"

    session_manager._cow_clone(src, dst)

    assert (dst / "pkg" / "app.py").read_text(encoding="utf-8") == "SECRET = 'x'\n"
    # Writing into the clone must not affect the source.
    (dst / "pkg" / "app.py").write_text("MODIFIED\n", encoding="utf-8")
    assert (src / "pkg" / "app.py").read_text(encoding="utf-8") == "SECRET = 'x'\n"


def test_cow_clone_overwrites_existing_dst_without_nesting(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a\n", encoding="utf-8")
    dst = tmp_path / "workspace"
    dst.mkdir()
    (dst / "stale.py").write_text("stale\n", encoding="utf-8")

    session_manager._cow_clone(src, dst)

    # dst is the clone of src (no dst/src/... nesting), stale content gone.
    assert (dst / "a.py").read_text(encoding="utf-8") == "a\n"
    assert not (dst / "stale.py").exists()
    assert not (dst / "src").exists()


async def test_create_local_isolate_uses_clone(tmp_path: Path, monkeypatch: Any) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print(1)\n", encoding="utf-8")

    captured: dict[str, Any] = {}

    async def _fake_backend(*, image: str, manifest: Any, exposed_ports: Any, bind_mounts: Any):
        captured["root"] = str(manifest.root)
        return object(), object()

    monkeypatch.setattr(session_manager, "get_backend", lambda _name: _fake_backend)
    monkeypatch.setattr(session_manager, "run_dir_for", lambda _sid: tmp_path / "run")
    _force_isolate(monkeypatch, enabled=True)

    bundle = await session_manager._create_local(
        "scan-x", local_sources=[_source("repo", str(repo))]
    )

    clone = tmp_path / "run" / "workspace"
    assert bundle["workspace_root"] == str(clone)
    assert bundle["workspace_clone"] == str(clone)
    assert captured["root"] == str(clone)
    assert (clone / "main.py").read_text(encoding="utf-8") == "print(1)\n"
    assert repo.exists()  # original untouched
