---
name: source-aware-whitebox
description: Coordination playbook for source-aware white-box testing with static triage and dynamic validation
---

# Source-Aware White-Box Coordination

Use this coordination playbook when repository source code is available.

## Objective

Increase white-box coverage by combining source-aware triage with dynamic validation. Source-aware tooling is expected by default when source is available.

## Recommended Workflow

1. Build a quick source map before deep exploitation, including at least one AST-structural pass (`sg` or `tree-sitter`) scoped to relevant paths.
   - For `sg` baseline, derive `sg-targets.txt` from `semgrep.json` scope first (`paths.scanned`, fallback to unique `results[].path`) and run `xargs ... sg run` on that list.
   - Only fall back to path heuristics when semgrep scope is unavailable.
2. **Seed the coverage manifest** from the semgrep report with
   `seed_coverage_from_semgrep`, then register routes/handlers/sinks with
   `add_coverage_units`. Every unit must later be dispositioned — the recall
   gate blocks `finish_scan` while any unit is `pending`.
3. Run first-pass static triage to rank high-risk paths.
4. Run the **sink → source taint pass** (see `source_aware_sast`): enumerate
   sinks, trace each back to a source, check sanitizers in between.
5. Use triage outputs to prioritize dynamic PoC validation.
6. Keep findings evidence-driven: report on a clear, traceable source-to-sink
   flow with a concrete PoC payload; add dynamic validation when the environment
   allows, but do not gate reporting on it.

## Cross-File Review With `trace_symbol`

A sink's safety usually cannot be judged from its own file — whether its inputs
are trusted depends on its **callers**, which often live elsewhere (a handler, a
middleware, another service). Do not review a function in isolation.

When you hit a dangerous sink or an auth-sensitive function:

- `trace_symbol(symbol=<name>, direction=callers)` to find every call site (the
  upstream where untrusted input enters or where an auth check should be).
- `trace_symbol(symbol=<name>, direction=definition)` to jump to where it is
  defined.
- Open the returned `file:line` locations (you have filesystem access) to read
  full bodies and confirm whether tainted data actually reaches the sink.

`trace_symbol` is a fast locator, not a data-flow engine: it finds the chain;
you read the code to confirm the flow.

## Source-Aware Triage Stack

- `semgrep`: fast security-first triage and custom pattern scans
- `ast-grep` (`sg`): structural pattern hunting and targeted repo mapping
- `tree-sitter`: syntax-aware parsing support for symbol and route extraction
- `gitleaks` + `trufflehog`: complementary secret detection (working tree and history coverage)
- `trivy fs`: dependency, misconfiguration, license, and secret checks

Coverage target per repository:
- one `semgrep` pass
- one AST structural pass (`sg` and/or `tree-sitter`)
- one secrets pass (`gitleaks` and/or `trufflehog`)
- one `trivy fs` pass

## Agent Delegation Guidance

- Keep child agents specialized by vulnerability/component as usual.
- For source-heavy subtasks, prefer creating child agents with `source_aware_sast` skill.
- Use source findings to shape payloads and endpoint selection for dynamic testing.

## Validation Guardrails

- A finding is reportable on strong, traceable source evidence (a clear
  source-to-sink data flow, or a missing/incorrect authorization check) plus a
  concrete PoC payload in the report.
- Dynamic exploitation evidence strengthens a finding and is preferred when the
  environment is available — but do NOT withhold a well-evidenced source-level
  finding solely because dynamic execution was not possible.
- State confidence explicitly: source-confirmed vs dynamically-confirmed.
- Keep scanner output concise, deduplicated, and mapped to concrete code locations.
