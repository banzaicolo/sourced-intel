# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please do **not** open a public issue. Report it privately instead:

- Use GitHub's private vulnerability reporting: <https://github.com/banzaicolo/sourced-intel/security/advisories/new> (recommended), or
- Contact the maintainers directly.

Please include enough detail to reproduce the issue and the affected version(s). We aim to acknowledge reports within a few days and fix confirmed issues as soon as possible.

## What counts as a vulnerability

- Anything that leaks an API key, token, or other credential.
- Anything that lets a report embed untrusted script or content (XSS).
- Anything that exposes the REST API or its data beyond the intended boundary.

## Keeping keys safe

- Never commit a real API key. Copy `.env.example` to `.env` and fill in your own `GLM_API_KEY`.
- `.env` is git-ignored — do not force-add it.
- Every `POST /runs` spends real LLM quota, and the REST API has no default authentication. Keep it bound to `127.0.0.1` unless you have explicitly decided to expose it.
- If you do expose it, set `SOURCED_INTEL_API_TOKEN` (all routes except `/health` then require `Authorization: Bearer <token>`) and keep `MAX_CONCURRENT_RUNS` low so a loop cannot multiply the bill.
