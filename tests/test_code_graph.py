"""Tests for the trace_symbol call-chain locator's pure parsing/classification."""

from __future__ import annotations

from strix.tools.code_graph import tools as cg


def test_classify_definition() -> None:
    assert cg._classify("build_query", "def build_query(n):") == "definition"
    assert cg._classify("buildQuery", "function buildQuery(n) {") == "definition"
    assert cg._classify("Vault", "contract Vault is Ownable {") == "definition"


def test_classify_caller() -> None:
    assert cg._classify("build_query", "    q = build_query(name)") == "caller"


def test_classify_reference() -> None:
    assert cg._classify("build_query", "# build_query is used downstream") == "reference"


def test_classify_caller_inside_other_definition() -> None:
    # The line defines `handler` but *calls* build_query — for build_query it is
    # a caller, not a definition.
    line = "def handler(): return build_query(x)"
    assert cg._classify("build_query", line) == "caller"


def test_parse_grep_output_buckets() -> None:
    stdout = (
        "db.py:88:def build_query(n):\n"
        "views.py:12:    q = build_query(name)\n"
        "README.md:3:build_query explained here\n"
        "malformed line without colons\n"
    )
    res = cg._parse_grep_output("build_query", stdout, max_per_bucket=40)
    assert [d["location"] for d in res["definitions"]] == ["db.py:88"]
    assert [c["location"] for c in res["callers"]] == ["views.py:12"]
    assert [r["location"] for r in res["references"]] == ["README.md:3"]


def test_parse_grep_output_respects_cap() -> None:
    stdout = "".join(f"f{i}.py:{i}:    build_query(a)\n" for i in range(10))
    res = cg._parse_grep_output("build_query", stdout, max_per_bucket=3)
    assert len(res["callers"]) == 3


def test_build_grep_command_quotes_path_and_excludes() -> None:
    cmd = cg._build_grep_command("build_query", "src/api", "/workspace")
    assert "\\bbuild_query\\b" in cmd
    assert "src/api" in cmd
    assert "--exclude-dir=node_modules" in cmd
    assert "cd /workspace &&" in cmd


def test_build_grep_command_honors_local_workspace_root() -> None:
    # Under the local backend the root is a real host path, and it must be
    # shell-quoted so paths with spaces / metacharacters can't break the cd.
    cmd = cg._build_grep_command("x", ".", "/Users/me/my repo")
    assert "cd '/Users/me/my repo' &&" in cmd


def test_build_grep_command_neutralizes_shell_metacharacters() -> None:
    # shlex.quote must prevent variable expansion / quote breakout on odd paths.
    cmd = cg._build_grep_command("x", "a/$HOME/b", "/workspace")
    assert "$HOME" in cmd  # present literally...
    assert "'a/$HOME/b'" in cmd  # ...inside single quotes, so the shell won't expand it

    cmd2 = cg._build_grep_command("x", 'a"b', "/workspace")
    # the path is single-quoted, so the embedded double quote cannot break out
    assert "'a\"b'" in cmd2
