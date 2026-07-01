"""Coverage manifest — scan-wide attack-surface units, mirrored to {state_dir}/coverage.json.

The manifest turns "did we review the whole attack surface?" from an LLM judgement
call into a countable, gate-able checklist. Recon enumerates units (routes, handlers,
dangerous sinks, contract functions, entrypoints); every unit must be explicitly
*dispositioned* (``reviewed`` or ``ruled_out``) before ``finish_scan`` is allowed.

Global (scan-wide) storage, mirroring ``notes`` rather than ``todo`` — coverage is a
property of the whole scan, not of one agent, and every agent contributes to and reads
the same manifest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool


logger = logging.getLogger(__name__)


VALID_KINDS = ["route", "handler", "sink", "contract_fn", "entrypoint", "file"]
# "pending" units block finish_scan; the two terminal dispositions clear the gate.
VALID_DISPOSITIONS = ["reviewed", "ruled_out"]
PENDING_STATUS = "pending"

_WORKSPACE_ROOT = "/workspace"

_coverage_storage: dict[str, dict[str, Any]] = {}
_coverage_lock = threading.RLock()
_coverage_path: Path | None = None


def hydrate_coverage_from_disk(state_dir: Path) -> None:
    """Point storage at ``{state_dir}/coverage.json`` and load any prior state.

    Called once per scan from the runner (alongside notes/todos hydration) so a
    resumed scan keeps its manifest and the gate still sees prior pending units.
    """
    global _coverage_path  # noqa: PLW0603
    _coverage_path = state_dir / "coverage.json"
    with _coverage_lock:
        _coverage_storage.clear()
        if not _coverage_path.exists():
            return
        try:
            data = json.loads(_coverage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "coverage.json at %s is unreadable; starting with empty manifest",
                _coverage_path,
            )
            return
        if not isinstance(data, dict):
            return
        _coverage_storage.update(
            {
                uid: unit
                for uid, unit in data.items()
                if isinstance(uid, str) and isinstance(unit, dict)
            }
        )
        logger.info(
            "coverage manifest hydrated from %s (%d unit(s))",
            _coverage_path,
            len(_coverage_storage),
        )


def _persist() -> None:
    path = _coverage_path
    if path is None:
        return
    try:
        payload = json.dumps(_coverage_storage, ensure_ascii=False, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _coverage_lock,
            tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp,
        ):
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)
    except Exception:
        logger.exception("coverage persist to %s failed", path)


def _unit_dedupe_key(kind: str, location: str) -> str:
    return f"{kind}::{location.strip()}"


def _existing_keys() -> dict[str, str]:
    """Map dedupe-key -> unit_id for everything already in the manifest."""
    return {
        _unit_dedupe_key(unit.get("kind", ""), unit.get("location", "")): uid
        for uid, unit in _coverage_storage.items()
    }


def _normalize_units(raw_units: Any) -> list[dict[str, Any]]:
    """Accept a JSON string, a list of dicts, or a single dict — like ``todo``."""
    if raw_units is None:
        return []
    data: Any = raw_units
    if isinstance(raw_units, str):
        stripped = raw_units.strip()
        if not stripped:
            return []
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise ValueError("Units must be valid JSON") from e
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise TypeError("Units must be a list of unit objects")

    normalized: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise TypeError("Each unit must be an object with 'kind' and 'location'")
        kind = str(item.get("kind", "")).strip().lower()
        location = str(item.get("location", "")).strip()
        if kind not in VALID_KINDS:
            raise ValueError(f"Invalid kind '{kind}'. Must be one of: {', '.join(VALID_KINDS)}")
        if not location:
            raise ValueError("Each unit must include a non-empty 'location'")
        title = str(item.get("title", "")).strip() or location
        normalized.append({"kind": kind, "location": location, "title": title})
    return normalized


def _normalize_ids(raw_ids: Any) -> list[str]:
    if raw_ids is None:
        return []
    if isinstance(raw_ids, str):
        stripped = raw_ids.strip()
        if not stripped:
            return []
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = stripped.split(",") if "," in stripped else [stripped]
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
        return [str(data).strip()]
    if isinstance(raw_ids, list):
        return [str(item).strip() for item in raw_ids if str(item).strip()]
    return [str(raw_ids).strip()]


def _summary() -> dict[str, int]:
    counts = {"total": len(_coverage_storage), PENDING_STATUS: 0}
    for status in VALID_DISPOSITIONS:
        counts[status] = 0
    for unit in _coverage_storage.values():
        status = unit.get("status", PENDING_STATUS)
        counts[status] = counts.get(status, 0) + 1
    return counts


def pending_units() -> list[dict[str, Any]]:
    """Units still awaiting disposition — consumed by the finish_scan gate."""
    with _coverage_lock:
        return [
            {**unit, "unit_id": uid}
            for uid, unit in _coverage_storage.items()
            if unit.get("status", PENDING_STATUS) == PENDING_STATUS
        ]


def manifest_is_empty() -> bool:
    with _coverage_lock:
        return not _coverage_storage


def _add_units_impl(units: Any, source: str) -> dict[str, Any]:
    with _coverage_lock:
        try:
            parsed = _normalize_units(units)
        except (ValueError, TypeError) as e:
            return {"success": False, "error": str(e), "added": 0}
        if not parsed:
            return {"success": False, "error": "No units provided", "added": 0}

        existing = _existing_keys()
        added: list[str] = []
        skipped = 0
        timestamp = datetime.now(UTC).isoformat()
        for unit in parsed:
            key = _unit_dedupe_key(unit["kind"], unit["location"])
            if key in existing:
                skipped += 1
                continue
            unit_id = str(uuid.uuid4())[:6]
            _coverage_storage[unit_id] = {
                "kind": unit["kind"],
                "location": unit["location"],
                "title": unit["title"],
                "status": PENDING_STATUS,
                "disposition_note": "",
                "source": source,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            existing[key] = unit_id
            added.append(unit_id)
        _persist()
        return {
            "success": True,
            "added": len(added),
            "skipped_duplicates": skipped,
            "unit_ids": added,
            "summary": _summary(),
        }


def _mark_reviewed_impl(unit_ids: Any, status: str, note: str) -> dict[str, Any]:
    with _coverage_lock:
        status_norm = (status or "").strip().lower()
        if status_norm not in VALID_DISPOSITIONS:
            return {
                "success": False,
                "error": f"Invalid status. Must be one of: {', '.join(VALID_DISPOSITIONS)}",
            }
        if not note or not note.strip():
            return {
                "success": False,
                "error": "A disposition note is required (what you checked / why it's ruled out)",
            }
        ids = _normalize_ids(unit_ids)
        if not ids:
            return {"success": False, "error": "No unit_ids provided"}

        updated: list[str] = []
        missing: list[str] = []
        timestamp = datetime.now(UTC).isoformat()
        for uid in ids:
            unit = _coverage_storage.get(uid)
            if unit is None:
                missing.append(uid)
                continue
            unit["status"] = status_norm
            unit["disposition_note"] = note.strip()
            unit["updated_at"] = timestamp
            updated.append(uid)
        _persist()
        return {
            "success": not missing or bool(updated),
            "updated": updated,
            "missing": missing,
            "summary": _summary(),
        }


def _list_impl(status: str | None = None) -> dict[str, Any]:
    with _coverage_lock:
        status_filter = (status or "").strip().lower() or None
        units = [
            {**unit, "unit_id": uid}
            for uid, unit in _coverage_storage.items()
            if status_filter is None or unit.get("status", PENDING_STATUS) == status_filter
        ]
        units.sort(key=lambda u: (u.get("status", ""), u.get("kind", ""), u.get("location", "")))
        return {
            "success": True,
            "units": units,
            "filtered_count": len(units),
            "summary": _summary(),
        }


def _decode_stream(result: Any, attr: str) -> str:
    raw = getattr(result, attr, None)
    return raw.decode("utf-8", errors="replace") if raw else ""


async def _seed_from_semgrep_impl(  # noqa: PLR0911
    ctx_inner: dict[str, Any], semgrep_json_path: str
) -> dict[str, Any]:
    session = ctx_inner.get("sandbox_session")
    if session is None:
        return {"success": False, "error": "No sandbox session in context", "added": 0}
    workspace_root = ctx_inner.get("workspace_root") or _WORKSPACE_ROOT
    safe_path = semgrep_json_path.strip()
    if not safe_path:
        return {"success": False, "error": "semgrep_json_path is empty", "added": 0}
    try:
        result = await session.exec(
            "bash",
            "-lc",
            f"cd {shlex.quote(workspace_root)} && cat -- {shlex.quote(safe_path)}",
            timeout=30,
        )
    except Exception as e:
        logger.exception("seed_coverage_from_semgrep: exec failed")
        return {"success": False, "error": f"Failed to read semgrep json: {e!s}", "added": 0}

    stdout = _decode_stream(result, "stdout")
    if getattr(result, "exit_code", 1) != 0 or not stdout.strip():
        exit_code = getattr(result, "exit_code", "?")
        stderr = _decode_stream(result, "stderr")
        return {
            "success": False,
            "error": f"Could not read {safe_path} (exit={exit_code}): {stderr[:200]}",
            "added": 0,
        }
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"semgrep json is not valid JSON: {e!s}", "added": 0}

    paths: list[str] = []
    scanned = (((data.get("paths") or {}).get("scanned")) if isinstance(data, dict) else None) or []
    if isinstance(scanned, list) and scanned:
        paths = [str(p) for p in scanned]
    else:
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, list):
            seen: set[str] = set()
            for r in results:
                p = str((r or {}).get("path", "")).strip() if isinstance(r, dict) else ""
                if p and p not in seen:
                    seen.add(p)
                    paths.append(p)
    if not paths:
        return {"success": True, "added": 0, "message": "No scanned paths found in semgrep json"}

    units = [{"kind": "file", "location": p, "title": p} for p in paths]
    return await asyncio.to_thread(_add_units_impl, units, "seed")


@function_tool(timeout=30)
async def add_coverage_units(ctx: RunContextWrapper, units: str) -> str:
    """Register attack-surface units in the scan-wide coverage manifest.

    Use this during/after reconnaissance to record everything that must be reviewed:
    routes, request handlers, dangerous sinks, smart-contract functions, and entrypoints.
    Every registered unit later has to be dispositioned via ``mark_unit_reviewed`` before
    the scan can finish — so register the real attack surface, not noise.

    Duplicate units (same ``kind`` + ``location``) are skipped automatically, so it is
    safe to register incrementally and to re-run enumeration.

    Args:
        units: A JSON array of unit objects. Each object needs:
            - ``kind``: one of route | handler | sink | contract_fn | entrypoint | file
            - ``location``: ``"path/to/file.py:42"`` or a path / route string
            - ``title`` (optional): short human label; defaults to ``location``
            Example:
            ``[{"kind":"sink","location":"db.py:88","title":"raw SQL in build_query"},
            {"kind":"route","location":"views.py:12","title":"GET /search"}]``
    """
    return json.dumps(
        await asyncio.to_thread(_add_units_impl, units, "agent"),
        ensure_ascii=False,
        default=str,
    )


@function_tool(timeout=30)
async def mark_unit_reviewed(ctx: RunContextWrapper, unit_ids: str, status: str, note: str) -> str:
    """Disposition one or more coverage units after reviewing them.

    This is how the coverage gate is cleared. ``reviewed`` means you actively analyzed
    the unit (regardless of whether a vulnerability was found — file any finding via
    ``create_vulnerability_report`` separately). ``ruled_out`` means the unit is not a
    real attack surface or is out of scope. A note is mandatory: state what you checked
    or why it is ruled out.

    Args:
        unit_ids: A unit id, a comma-separated list, or a JSON array of ids
            (from ``list_coverage`` / ``add_coverage_units``).
        status: ``reviewed`` or ``ruled_out``.
        note: Short justification — what you analyzed, or why it's ruled out.
    """
    return json.dumps(
        await asyncio.to_thread(_mark_reviewed_impl, unit_ids, status, note),
        ensure_ascii=False,
        default=str,
    )


@function_tool(timeout=30)
async def list_coverage(ctx: RunContextWrapper, status: str | None = None) -> str:
    """List coverage units and the reviewed/total summary.

    Call this to see what is still ``pending`` before attempting ``finish_scan`` — the
    finish gate blocks while any unit is pending.

    Args:
        status: Optional filter — ``pending`` | ``reviewed`` | ``ruled_out``.
    """
    return json.dumps(
        await asyncio.to_thread(_list_impl, status),
        ensure_ascii=False,
        default=str,
    )


@function_tool(timeout=60)
async def seed_coverage_from_semgrep(ctx: RunContextWrapper, semgrep_json_path: str) -> str:
    """Seed file-level coverage units from a semgrep JSON report.

    Run after your semgrep pass to bootstrap the manifest deterministically: reads
    ``paths.scanned`` (falling back to the unique ``results[].path``) from the report in
    the sandbox and registers one ``kind=file`` unit per scanned file. This guarantees a
    floor of coverage that does not depend on the agent remembering every file. Add the
    finer-grained ``sink`` / ``route`` units yourself via ``add_coverage_units``.

    Args:
        semgrep_json_path: Path to the semgrep JSON output inside the sandbox,
            relative to ``/workspace`` or absolute (e.g. ``"semgrep.json"``).
    """
    inner = ctx.context if isinstance(getattr(ctx, "context", None), dict) else {}
    return json.dumps(
        await _seed_from_semgrep_impl(inner, semgrep_json_path),
        ensure_ascii=False,
        default=str,
    )
