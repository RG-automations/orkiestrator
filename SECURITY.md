# Security model

This is a portfolio project, not a maintained product. There is no bug bounty and no SLA. If you find a
real issue, open a GitHub issue or a private security advisory on this repo.

## What the gate actually guarantees

The state machine in [`gate.py`](src/orkiestrator/gate.py) is the only code path that changes a post's
status. It enforces, and [`tests/test_hitl.py`](tests/test_hitl.py) checks:

- Only an actor string starting with `human:` (and nothing shorter) can `approve` or `reject`.
- `schedule` and `publish` re-verify the human approval on the row itself (not just the status column)
  immediately before any platform call, including that `approved_hash` still matches a SHA-256 of the
  platform-facing fields (`platform`, `kind`, `title`, `hook`, `body`, `payload`, `link_id`). A row with a
  forged `status`/`approved_by`, or content changed after approval by anything outside the gate (a raw
  SQL `UPDATE`, a migration, a bug), is refused.
- Editing a post's body clears its approval; it has to be approved again.
- Every transition is written to the `events` table with its real actor, so the full approve/reject/edit
  history survives even if a post is later deleted from the UI's point of view (archiving, not deletion).

This is **defense in depth on a single-writer SQLite file**, not a security boundary against someone who
already has write access to that file or to the Python process. Anyone who can run arbitrary code with
the same file permissions as the owner can bypass all of this. The threat model is: an AI agent, or a bug
in the AI pipeline, must not be able to publish without a human clicking a button.

## The owner panel

- Binds to `127.0.0.1` by default. `ORKI_HOST` can expose it on the network, but `serve()` then refuses to
  start unless `ORKI_TOKEN` is set explicitly (a random token would otherwise land in `docker logs`).
- The session cookie is `HttpOnly`, `SameSite=Strict`, and compared with `hmac.compare_digest`.
- `POST` requests are rejected if `Origin` doesn't match `Host` (CSRF).
- **The token passed as `?token=` in the URL is also the session secret.** It survives in browser history
  and in any proxy or server access log that records full URLs. Treat the link as a credential: don't
  paste it into chat, a public channel, or a URL shortener. Rotating it means restarting the process with
  a new `ORKI_TOKEN` (or letting it generate a new random one).
- `GET /r/<id>` and `GET /healthz` are intentionally public: the first is the click-tracking redirect
  (counts a click, never returns queue data), the second is an uptime check that returns no data.

## Publishing

- Every platform other than Facebook is dry-run only: a "would-be post" is written to `data/outbox/` as
  JSON. There is no code path to a real Instagram, YouTube, LinkedIn or TikTok API in this repo.
- The Facebook adapter needs `FB_PAGE_ID` and `FB_PAGE_TOKEN` **and** the owner typing the literal string
  `OPUBLIKUJ NAPRAWDE` into the panel. That live flag lives only in the server process's memory and
  resets on every restart — it is a deliberate manual safety switch, not a stored setting.
- The Facebook adapter is tested against a mock HTTP call in this repo. It has not been exercised against
  the real Graph API here; treat it as unverified until you test it yourself against a test Page.

## Secrets

- `local.env` holds API keys and is in `.gitignore`. Never commit it; `local.env.example` has empty
  values only.
- `gitleaks` runs in CI (`secrets` job) against the full history on every push.
- The demo knowledge and input files (`knowledge/`, `inputs/`) describe a fictional coffee roastery. No
  real prices, customers, or brand belongs in this repo.

## Known limitations

- SQLite is a single file with no row-level locking beyond what SQLite itself provides; this is a
  single-owner tool, not a multi-tenant system.
- `data/` (the database, backups, outbox) is not encrypted at rest.
- There is no rate limiting on the panel beyond the OS/network layer.
