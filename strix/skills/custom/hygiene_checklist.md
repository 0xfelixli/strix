---
name: hygiene-checklist
description: Language-agnostic weakness checklist for config, crypto, session/cookie, tokens, enumeration, template XSS, CSRF — the non-exploit-chain classes an attack-path lens tends to miss
---

# Security Hygiene Checklist (language-agnostic)

Your default lens hunts *exploitable attack chains* (RCE / SQLi / IDOR / auth bypass).
That lens systematically **misses static weaknesses** — misconfiguration, weak crypto,
cookie/session flags, low-entropy tokens, user enumeration, template/output XSS, CSRF.
These are real findings and count for coverage.

Rules for this class:
- **Report each as its own finding**, even Medium/Low/best-practice. Do not fold them
  into a nearby high-sev report and do not skip them because there's no runnable exploit.
- `poc_script_code` may be empty for these; `poc_description` is still required — describe
  the concrete attack path / why it's dangerous.
- These concepts are **language-independent** — apply them to any language, including ones
  semgrep has no ruleset for (Dart, Kotlin, Swift, …). Read the code and check each item.
- When you review a security-sensitive file (settings/config, crypto utils, auth/session,
  templates), your `mark_unit_reviewed` note must state **what you found or explicitly "none"** —
  never mark reviewed without extracting issues.

## 1. Configuration & secrets
- Debug/verbose mode on in production (`DEBUG=True`, stack traces, dev error pages)
- Wildcard / missing host allowlist (`ALLOWED_HOSTS=['*']`, permissive CORS `*`)
- Hardcoded framework secret / signing key (`SECRET_KEY`, JWT secret) in source
- Hardcoded DB / service / cloud credentials (settings, Docker/compose, CI, seed data)
- Default or seeded accounts & passwords; test/demo creds committed
- Plaintext credentials exposed in public templates/pages/JS

## 2. Weak cryptography
- Weak/fast hash for passwords (MD5, SHA1, unsalted) instead of bcrypt/argon2/scrypt
- Deprecated / broken crypto libs (e.g. PyCrypto) or algorithms (DES, RC4, ECB)
- AES-CBC (or any mode) with static / reused / predictable IV or nonce
- Custom padding / unpad logic → padding-oracle risk; MAC-then-encrypt / no integrity
- Low-entropy or homegrown randomness for security values (not a CSPRNG)

## 3. Session & cookies
- Auth/session cookies missing `Secure`, `HttpOnly`, or `SameSite`
- Session fixation: session/token **not rotated on login / privilege change**
- Long-lived or non-expiring sessions/tokens; no server-side invalidation on logout

## 4. Tokens & secrets quality
- Predictable / low-entropy tokens (derived from MD5, timestamps, small random space)
- Password-reset / verification tokens: guessable, no expiry, reusable, not single-use

## 5. Account enumeration
- Login / signup / password-reset responses (message or timing) reveal whether a
  username/email exists

## 6. Output / template XSS (beyond the obvious reflected case)
- Template rendering user data into JS context / event handlers / URLs
- Unquoted HTML attribute injection contexts
- Reflected XSS via URL path / query params echoed into pages
- DOM XSS: `.html()`, `innerHTML`, `document.write` on attacker-influenced values

## 7. CSRF
- State-changing endpoints without CSRF protection, disabled CSRF, or a **typo'd /
  mismatched CSRF token field** that silently disables the check

---

Sweep every item against the target regardless of language. If semgrep flagged some of
these, still verify and file them; if semgrep could not (unsupported language), this
checklist is the primary safety net.
