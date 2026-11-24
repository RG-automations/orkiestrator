# Changelog

## 0.1.0 — 2026-11-24

First public version, extracted from a private project with a fictional demo brand.

- Approval gate (`gate.py`): single state machine, human-only approve/reject, content-hash binding so a
  post can't be published if it changed after approval.
- Owner web panel: queue, alerts, leads, setup wizard, morning loop.
- AI pipeline: research brief, content drafts, replies — any of Gemini, Claude, or an offline demo
  provider (`ORKI_PROVIDER=demo`) that needs no key and no network.
- Dry-run publishing for every platform; an opt-in Facebook adapter behind an explicit live-mode
  confirmation.
- 139 tests, including negative tests for the approval gate (forged status, forged approval, content
  tampering, non-human actors) and an end-to-end demo run on a throwaway copy of the repo.
- CI: tests on Python 3.10–3.13, ruff, gitleaks.
