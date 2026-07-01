"""Per-scan sandbox session lifecycle."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agents.sandbox.entries import BaseEntry, LocalDir
from agents.sandbox.manifest import Environment, Manifest

from strix.config import load_settings
from strix.core.paths import run_dir_for
from strix.runtime.backends import get_backend


logger = logging.getLogger(__name__)


_SESSION_CACHE: dict[str, dict[str, Any]] = {}

# Manifest root inside the container; entry keys hang off this path.
_WORKSPACE_ROOT = "/workspace"


def build_session_entries(
    local_sources: list[dict[str, Any]],
) -> tuple[dict[str | Path, BaseEntry], list[dict[str, Any]]]:
    """Split local sources into copied manifest entries and host bind mounts.

    Sources flagged ``mount`` are bind-mounted read-only at
    ``/workspace/<workspace_subdir>`` (not added to the manifest, so the SDK
    does not stream them in file-by-file). Every other source becomes a
    ``LocalDir`` entry copied into the container as before.
    """
    entries: dict[str | Path, BaseEntry] = {}
    bind_mounts: list[dict[str, Any]] = []
    for src in local_sources:
        ws_subdir = src.get("workspace_subdir") or ""
        host_path = src.get("source_path") or ""
        if not ws_subdir or not host_path:
            continue
        resolved = Path(host_path).expanduser().resolve()
        if src.get("mount"):
            bind_mounts.append(
                {
                    "source": str(resolved),
                    "target": f"{_WORKSPACE_ROOT}/{ws_subdir}",
                    "read_only": True,
                }
            )
        else:
            entries[ws_subdir] = LocalDir(src=resolved)
    return entries, bind_mounts


async def create_or_reuse(
    scan_id: str,
    *,
    image: str,
    local_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the existing session bundle for ``scan_id`` or create a new one.

    Each ``local_sources`` entry exposes its host ``source_path`` at
    ``/workspace/<workspace_subdir>`` inside the container — copied in, or
    bind-mounted read-only when the entry is flagged ``mount``.
    """
    cached = _SESSION_CACHE.get(scan_id)
    if cached is not None:
        logger.info("Reusing existing sandbox session for scan %s", scan_id)
        return cached

    backend_name = load_settings().runtime.backend

    if backend_name == "local":
        bundle = await _create_local(scan_id, local_sources=local_sources)
    else:
        bundle = await _create_containerized(
            scan_id, image=image, local_sources=local_sources, backend_name=backend_name
        )

    _SESSION_CACHE[scan_id] = bundle
    logger.info("Sandbox session for scan %s ready and cached", scan_id)
    return bundle


async def _create_containerized(
    scan_id: str,
    *,
    image: str,
    local_sources: list[dict[str, Any]],
    backend_name: str,
) -> dict[str, Any]:
    """Bring up a container-backed session (Docker). Static-audit build: no Caido/proxy."""
    entries, bind_mounts = build_session_entries(local_sources)

    manifest = Manifest(
        entries=entries,
        environment=Environment(
            value={
                "PYTHONUNBUFFERED": "1",
                "HOST_GATEWAY": "host.docker.internal",
            },
        ),
    )

    backend = get_backend(backend_name)

    logger.info(
        "Creating sandbox session for scan %s (backend=%s, image=%s)",
        scan_id,
        backend_name,
        image,
    )
    client, session = await backend(
        image=image,
        manifest=manifest,
        exposed_ports=(),
        bind_mounts=bind_mounts,
    )

    await _run_setup_command(session, workspace_root=_WORKSPACE_ROOT)

    return {
        "client": client,
        "session": session,
        "workspace_root": _WORKSPACE_ROOT,
        "workspace_clone": None,
    }


def _cow_clone(src: Path, dst: Path) -> None:
    """Copy-on-write clone ``src`` to ``dst`` (APFS clonefile / reflink; plain copy fallback).

    ``dst`` is removed first: ``cp -R src <existing-dst>`` would nest the source
    as ``dst/<name>/…`` instead of making ``dst`` the clone.
    """
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # macOS APFS clonefile, then Linux reflink, then a plain recursive copy for
    # cross-volume / non-CoW filesystems (correct but not space-shared).
    attempts = (["cp", "-c", "-R"], ["cp", "--reflink=auto", "-R"], ["cp", "-R"])
    for args in attempts:
        try:
            # Fixed flags + resolved paths; no shell, no untrusted tokens.
            subprocess.run(  # noqa: S603
                [*args, str(src), str(dst)], check=True, capture_output=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            shutil.rmtree(dst, ignore_errors=True)
            continue
        logger.info("Cloned source via %s: %s -> %s", " ".join(args), src, dst)
        return
    raise RuntimeError(f"failed to clone source tree {src} -> {dst}")


async def _create_local(
    scan_id: str,
    *,
    local_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bring up a host-local session for static/whitebox review — no Docker.

    Zero-copy: ``manifest.root`` points straight at the source tree, so the
    agent's ``session.exec`` runs with its cwd inside the real repo and no
    files are streamed anywhere. No sandbox sidecar / exposed ports.
    """
    usable = [src for src in local_sources if (src.get("source_path") or "").strip()]
    if len(usable) != 1:
        raise RuntimeError(
            "STRIX_RUNTIME_BACKEND=local supports exactly one local source "
            f"(got {len(usable)}). Point --target at a single directory, or use "
            "the docker backend for multi-target / mounted scans."
        )

    source_root = Path(usable[0]["source_path"]).expanduser().resolve()

    # STRIX_LOCAL_ISOLATE: scan a CoW clone so the agent's writes never touch the
    # real repo. Off by default → zero-copy in place (root = the source tree).
    clone_root: Path | None = None
    if load_settings().runtime.local_isolate:
        clone_root = run_dir_for(scan_id) / "workspace"
        _cow_clone(source_root, clone_root)
        workspace_root = str(clone_root)
    else:
        workspace_root = str(source_root)

    # No entries → session.start() only mkdirs the (already-existing) root; no copy.
    manifest = Manifest(
        root=workspace_root,
        environment=Environment(value={"PYTHONUNBUFFERED": "1"}),
    )

    backend = get_backend("local")
    logger.info(
        "Creating local (host) session for scan %s (workspace_root=%s)",
        scan_id,
        workspace_root,
    )
    client, session = await backend(
        image="",
        manifest=manifest,
        exposed_ports=(),
        bind_mounts=[],
    )

    await _run_setup_command(session, workspace_root=workspace_root)

    return {
        "client": client,
        "session": session,
        "workspace_root": workspace_root,
        "workspace_clone": str(clone_root) if clone_root else None,
    }


async def _run_setup_command(session: Any, *, workspace_root: str = _WORKSPACE_ROOT) -> None:
    """Run ``STRIX_SETUP_CMD`` once in the sandbox before agents start.

    Best-effort: a non-zero exit is logged loudly but does not abort the scan.
    The command runs through ``bash -lc`` from ``workspace_root`` (``/workspace``
    in a container, the real repo path under the local backend), so it inherits
    the manifest env exactly like agent shells do.
    """
    settings = load_settings()
    cmd = (settings.runtime.setup_cmd or "").strip()
    if not cmd:
        return

    logger.info(
        "Running STRIX_SETUP_CMD in sandbox (timeout=%ss): %s",
        settings.runtime.setup_timeout,
        cmd,
    )
    try:
        result = await session.exec(
            "bash",
            "-lc",
            f"cd {workspace_root} && {cmd}",
            timeout=settings.runtime.setup_timeout,
        )
    except Exception:
        logger.exception("STRIX_SETUP_CMD raised; continuing without setup")
        return

    if result.ok():
        logger.info("STRIX_SETUP_CMD completed (exit 0)")
    else:
        stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
        logger.error("STRIX_SETUP_CMD failed (exit %s):\n%s", result.exit_code, stderr)


async def cleanup(scan_id: str) -> None:
    """Tear down ``scan_id``'s container and drop its cache entry.

    Best-effort: any error during ``client.delete`` is logged and
    swallowed. We never want a cleanup failure to prevent the next
    scan from starting; the worst case is a stranded container that
    Docker's normal reaping will catch on next ``docker prune``.
    """
    bundle = _SESSION_CACHE.pop(scan_id, None)
    if bundle is None:
        logger.debug("cleanup(%s): no cached session", scan_id)
        return

    try:
        await bundle["client"].delete(bundle["session"])
        logger.info("Cleaned up sandbox session for scan %s", scan_id)
    except Exception:
        logger.exception(
            "cleanup(%s): client.delete raised; container may need manual reaping",
            scan_id,
        )

    # CoW clone workspace (STRIX_LOCAL_ISOLATE) is kept after the scan so you can
    # inspect what the agent read/wrote. It lives under strix_runs/<scan>/workspace
    # and is removed manually (or when the whole run dir is cleaned).
    clone = bundle.get("workspace_clone")
    if clone:
        logger.info("cleanup(%s): keeping clone workspace %s", scan_id, clone)
