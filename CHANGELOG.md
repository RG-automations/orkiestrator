# Changelog

## 0.1.0 — 2026-11-24

First public version, extracted from a private project with a fictional demo brand.

- Approval gate (`gate.py`): single state machine, human-only approve/reject, content-hash binding so a
  post can't be published if it changed after approval.
- Publishing itself is a second, atomic gate (`gate.claim_for_publish`): a single `UPDATE ... WHERE`
  decides who gets to publish a given due post, so two concurrent publisher runs can't both publish it,
  and a post is frozen (un-editable) the moment it's claimed. A crashed claim is left stuck on purpose
  (not auto-retried) rather than risk a duplicate real publish; see `SECURITY.md`.
- Owner web panel: queue, alerts, leads, setup wizard, morning loop.
- AI pipeline: research brief, content drafts, replies — any of Gemini, Claude, or an offline demo
  provider (`ORKI_PROVIDER=demo`) that needs no key and no network.
- Dry-run publishing for every platform; an opt-in Facebook adapter behind an explicit live-mode
  confirmation.
- 148 tests, including negative tests for the approval gate (forged status, forged approval, content
  tampering, non-human actors, concurrent publish attempts, editing a post mid-publish), a set of
  injection/XSS checks (script tags, SQL metacharacters, template-injection strings through every text
  field), and an end-to-end demo run on a throwaway copy of the repo.
- CI: tests on Python 3.10–3.13, ruff, gitleaks.
- `assets/queue.png` (real screenshot of the demo panel) and `assets/social-preview.png` in the README.
