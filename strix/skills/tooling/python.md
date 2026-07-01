---
name: python
description: Run Python through exec_command in the sandbox for static-analysis helper scripts (parsing, AST, grep/collation).
---

# Python In The Sandbox

Use `exec_command` for Python. There is no separate Strix Python executor.

Prefer writing reusable scripts to a scratch dir (e.g. `scratch/<name>.py`) and
running them with `python3 scratch/<name>.py`. For short one-off transformations,
`python3 -c` or a small here-document is fine.

The `shell` parameter on `exec_command` is for swapping POSIX shells
(`bash`/`zsh`/`sh`), not for picking interpreters. Put the interpreter
invocation in `cmd` instead: `cmd="python3 -c '...'"`, not
`shell=python3, cmd="..."`. The `shell=<interpreter>` shortcut breaks
in subtle ways — `python3` works only with `login=False` (because the
SDK adds `-l`/`-i`), and other interpreters (`node`, `ruby`, `perl`)
take `-e` not `-c` so they fail even with `login=False`.

## Good uses in a code audit

Python is handy for the analysis work grep alone can't do:

- Parse `semgrep.json` / other tool output and collate findings.
- Walk the tree, extract routes/handlers/sinks, build target lists.
- Lightweight AST inspection (Python `ast`, or shell out to `ast-grep`/tree-sitter).
- De-dup / cross-reference findings across files.

```python
import json
from pathlib import Path

data = json.loads(Path("semgrep.json").read_text())
by_file = {}
for r in data.get("results", []):
    by_file.setdefault(r["path"], []).append(r["check_id"])
for path, rules in sorted(by_file.items()):
    print(path, len(rules))
```

## Installing extra packages

If a helper needs a package not already available, install it with `uv`
(faster than pip) or `pip` into the active Python environment, e.g.:

```bash
uv pip install <package>   # or: python3 -m pip install <package>
```
