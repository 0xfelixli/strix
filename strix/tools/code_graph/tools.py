"""Cross-file call-chain assembly for source-aware (whitebox) review.

A vulnerability's source and sink frequently live in different files: the auth
check in a middleware, the dangerous sink in a service. Reviewing a single file
in isolation hides those flows. ``trace_symbol`` locates a symbol's definition
and every call site across the repository so the finder sees the whole chain
instead of one function.

It runs ``grep`` inside the sandbox (always available, language-agnostic) and
classifies each hit into definitions / callers / references. The symbol is
strictly validated to an identifier, which also makes the grep pattern
injection-safe.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Any

from agents import RunContextWrapper, function_tool


logger = logging.getLogger(__name__)

_WORKSPACE_ROOT = "/workspace"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALID_DIRECTIONS = ("all", "definition", "callers")
# Declaration keywords across Python, JS/TS, Go, Rust, Solidity, Java/C-like.
_DEF_KEYWORDS = "def|function|func|fn|class|contract|interface|struct"
_EXCLUDE_DIRS = (
    ".git",
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
)
_GREP_LINE_CAP = 600


def _classify(symbol: str, line_text: str) -> str:
    """Bucket a matching line as 'definition', 'caller', or 'reference'.

    Definition = a declaration keyword closely followed by the symbol name
    (``def build_query``, ``function buildQuery``, ``contract Vault``) — the
    ``[^(\\n]`` guard stops ``def handler(): return build_query(x)`` from being
    mistaken for a definition of ``build_query``. Caller = a ``symbol(`` call.
    """
    esc = re.escape(symbol)
    if re.search(rf"\b(?:{_DEF_KEYWORDS})\b[^(\n]*?\b{esc}\b", line_text):
        return "definition"
    if re.search(rf"\b{esc}\s*\(", line_text):
        return "caller"
    return "reference"


def _parse_grep_output(symbol: str, stdout: str, max_per_bucket: int) -> dict[str, Any]:
    definitions: list[dict[str, Any]] = []
    callers: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        # grep -rn output: "path:line:content"
        parts = raw.split(":", 2)
        if len(parts) < 3:
            continue
        file_path, line_no, content = parts[0], parts[1], parts[2]
        if not line_no.isdigit():
            continue
        entry = {"location": f"{file_path}:{line_no}", "code": content.strip()[:240]}
        bucket = _classify(symbol, content)
        if bucket == "definition" and len(definitions) < max_per_bucket:
            definitions.append(entry)
        elif bucket == "caller" and len(callers) < max_per_bucket:
            callers.append(entry)
        elif bucket == "reference" and len(references) < max_per_bucket:
            references.append(entry)
    return {"definitions": definitions, "callers": callers, "references": references}


def _build_grep_command(symbol: str, path: str, workspace_root: str) -> str:
    excludes = " ".join(f"--exclude-dir={d}" for d in _EXCLUDE_DIRS)
    # shlex.quote gives correct POSIX-shell quoting for the path (no variable
    # expansion / quote-breakout on odd paths); symbol is already validated to a
    # bare identifier so it is safe inside the single-quoted grep pattern.
    quoted_path = shlex.quote(path)
    return (
        f"cd {shlex.quote(workspace_root)} && grep -rnE {excludes} "
        f"-e '\\b{symbol}\\b' {quoted_path} 2>/dev/null | head -n {_GREP_LINE_CAP}"
    )


async def _trace_symbol_impl(
    ctx_inner: dict[str, Any],
    symbol: str,
    direction: str,
    path: str,
    max_per_bucket: int,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not _IDENTIFIER_RE.match(symbol):
        return {
            "success": False,
            "error": "symbol must be a bare identifier (letters, digits, underscore)",
        }
    direction = (direction or "all").strip().lower()
    if direction not in _VALID_DIRECTIONS:
        return {
            "success": False,
            "error": f"direction must be one of: {', '.join(_VALID_DIRECTIONS)}",
        }
    session = ctx_inner.get("sandbox_session")
    if session is None:
        return {"success": False, "error": "No sandbox session in context"}

    workspace_root = ctx_inner.get("workspace_root") or _WORKSPACE_ROOT
    target_path = (path or ".").strip() or "."
    command = _build_grep_command(symbol, target_path, workspace_root)
    try:
        result = await session.exec("bash", "-lc", command, timeout=45)
    except Exception as e:
        logger.exception("trace_symbol: exec failed")
        return {"success": False, "error": f"grep failed: {e!s}"}

    raw_stdout = getattr(result, "stdout", None)
    stdout = raw_stdout.decode("utf-8", errors="replace") if raw_stdout else ""
    buckets = _parse_grep_output(symbol, stdout, max_per_bucket)

    if direction == "definition":
        buckets = {"definitions": buckets["definitions"], "callers": [], "references": []}
    elif direction == "callers":
        buckets = {
            "definitions": buckets["definitions"],
            "callers": buckets["callers"],
            "references": [],
        }

    total = len(buckets["definitions"]) + len(buckets["callers"]) + len(buckets["references"])
    return {
        "success": True,
        "symbol": symbol,
        "direction": direction,
        **buckets,
        "total_hits": total,
        "note": (
            "Locations are file:line. Open the relevant files (you have filesystem "
            "access) to read full function bodies and confirm whether tainted data "
            "reaches this symbol."
        )
        if total
        else "No matches — check the symbol spelling or widen the path.",
    }


@function_tool(timeout=60)
async def trace_symbol(
    ctx: RunContextWrapper,
    symbol: str,
    direction: str = "all",
    path: str = ".",
    max_per_bucket: int = 40,
) -> str:
    """Locate a symbol's definition and call sites across the repo (cross-file context).

    Use this during source-aware review to assemble a call chain instead of judging a
    function in isolation. When you find a dangerous sink (raw SQL, ``exec``, a transfer,
    a low-level ``call``), trace it to see **who calls it** — that is where untrusted
    input enters and where a missing auth/validation check actually lives.

    Returns matches grouped into:
    - ``definitions`` — where the symbol is defined (``def``/``function``/``func``/
      ``contract`` … lines).
    - ``callers`` — call sites (``symbol(`` …), i.e. the upstream of the chain.
    - ``references`` — other mentions.

    Each entry is ``{"location": "file:line", "code": "<line>"}``. Open the files for the
    full body (you have filesystem access). This is a fast locator, not a full data-flow
    engine — use it to find the chain, then read the code to confirm taint reaches a sink.

    Args:
        symbol: A bare identifier (function/method/class name). No expressions.
        direction: ``all`` (default), ``definition`` (only where it's defined), or
            ``callers`` (definition + call sites, skip loose references).
        path: Subtree to search, relative to ``/workspace`` (default ``"."`` = whole repo).
            Narrow it (e.g. ``"src/api"``) on large monorepos for speed.
        max_per_bucket: Cap per group (default 40).
    """
    inner = ctx.context if isinstance(ctx.context, dict) else {}
    return json.dumps(
        await _trace_symbol_impl(inner, symbol, direction, path, max_per_bucket),
        ensure_ascii=False,
        default=str,
    )
