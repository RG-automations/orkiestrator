# Security model

This is a portfolio project, not a maintained product. There is no bug bounty and no SLA. If you find a
real issue, open a GitHub issue or a private security advisory on this repo.

## Four distinctions this document does not blur

It is easy to read a few green checkmarks and a security doc and assume more coverage than exists. These
four pairs sound similar and are not the same thing. Everywhere below, read the narrower claim on the left.

- **Human-in-the-loop, not sandboxing.** The approval gate stops an AI-written post from reaching a
  platform without a click. It does not sandbox, contain, or limit what AI-generated *code* could do if
  the pipeline ever executed model output as code (it doesn't today — outputs are parsed as JSON text
  fields, never `eval`'d or shelled out to) or if an agent obtained arbitrary code execution in this
  environment. See "What actually enforces that an agent can't call approve/reject" below: that boundary
  is a permission-tool policy, not a code-level sandbox.
- **Escaping, not prompt-injection resistance.** `tests/test_injection.py` proves attacker-controlled text
  (script tags, SQL metacharacters, template-injection strings) can't break out of SQL, SVG or HTML — it
  stays inert data. It says nothing about whether a cleverly worded competitor comment or input file could
  talk the *language model itself* into drafting a bad, off-brand, or manipulative post. That risk is
  unchanged by any of this; it's why a human still reads every draft before approving it.
- **No duplicate internal call, not exactly-once external delivery.** `claim_for_publish()` guarantees the
  platform adapter is called **at most once** by this codebase for a given approved post (see "Publishing"
  below). It cannot guarantee the *external platform* only ever receives one copy: if the HTTP call to
  Facebook times out after the post was actually created server-side, this code sees a failure, releases
  the claim, and the next run retries — correctly, by this code's rules, and incorrectly, as a real
  duplicate on Facebook, because the Graph API's `/feed` endpoint takes no idempotency key. True
  exactly-once delivery needs platform-side idempotency support this project doesn't add.
- **Prototype, not production.** Single SQLite file, no deployment hardening, no monitoring, one
  integration tested against a mock. Extracted from a private project for portfolio purposes; see the
  README's Status line and "Known limitations" below before relying on this for anything real.

## What the gate actually guarantees

The state machine in [`gate.py`](src/orkiestrator/gate.py) is the only code path that changes a post's
status. It enforces, and [`tests/test_hitl.py`](tests/test_hitl.py) checks:

- Only an actor string starting with `human:` (and nothing shorter) can `approve` or `reject`.
- `schedule` and `publish` re-verify the human approval on the row itself (not just the status column)
  immediately before any platform call, including that `approved_hash` still matches a SHA-256 of the
  platform-facing fields (`platform`, `kind`, `title`, `hook`, `body`, `payload`, `link_id`). A row with a
  forged `status`/`approved_by`, or content changed after approval by anything outside the gate (a raw
  SQL `UPDATE`, a migration, a bug), is refused.
- Editing a post's body clears its approval; it has to be approved again. A post that is mid-publish
  (status `publishing`, see below) cannot be edited at all — not even by the owner — until that attempt
  resolves, so the content a publish call reads can never change under it.
- Every transition is written to the `events` table with its real actor, so the full approve/reject/edit
  history survives even if a post is later deleted from the UI's point of view (archiving, not deletion).

**Publishing itself is a second, narrower gate.** `due_posts()` is a plain read and can return the same
row to two concurrent callers (two publisher loops, or a loop racing a manual CLI `publish-due`).
`gate.claim_for_publish()` is a single atomic `UPDATE ... WHERE status IN ('approved','scheduled') AND
approved_hash=?`, so of two racing callers only one can actually move a given post to `publishing` —
SQLite serializes writers, so the loser's `UPDATE` matches zero rows once the winner commits. The hash in
that `WHERE` clause is recomputed from the row the caller just read, not taken from its own `approved_hash`
column, so a forged or stale `approved_hash` (set without the content actually matching) cannot satisfy
the claim either. The platform call always reads the row again right after the claim succeeds — the
content a `publishing` post is frozen on, because `edit_body()` refuses to touch it.

**If the process dies between the claim and a result (crash, kill, or an unhandled exception mid HTTP
call), the post is left in `publishing` on purpose, not auto-retried.** Automatically retrying could
publish it a second time if the first call actually went through before the crash; leaving it stuck is
the safer failure mode. `python -m orkiestrator list --status publishing` finds any post in that state;
resolving it (checking whether it actually went out, then manually moving it to `scheduled` or
`published`) is the owner's job, not the code's. This is a real, accepted limitation, not a bug.

This is **defense in depth on a single-writer SQLite file**, not a security boundary against someone who
already has write access to that file or to the Python process. Anyone who can run arbitrary code with
the same file permissions as the owner can bypass all of this. The threat model is: an AI agent, or a bug
in the AI pipeline, must not be able to publish without a human clicking a button.

**What actually enforces that an agent can't call `approve`/`reject` is the Claude Code permission layer
([`CLAUDE.md`](CLAUDE.md)'s hard rules plus the deny list in [`.claude/settings.json`](.claude/settings.json)),
not `gate.py` itself.** `gate.transition()`'s `is_human()` check only inspects the *shape* of the actor
string (`"human:" + something`) — it does not, and cannot, verify who or what actually produced that
string. The owner's two sanctioned paths (the CLI, which requires `sys.stdin.isatty()` before it will even
ask for confirmation, and the panel, which requires the session cookie) both compute the actor string
themselves rather than accepting it as input, so a real human has to be present to reach either. But code
with direct access to the `orkiestrator` package — an agent that obtained arbitrary code execution, or a
prompt-injected instruction that got the Claude Code agent to run `python3 -c "from orkiestrator import
gate; gate.transition(..., 'human:owner')"` instead of the disallowed CLI command — can call
`gate.transition()` with a forged `human:` actor directly, and nothing in `gate.py` will catch it. The
`.claude/settings.json` deny rules block the literal `python -m orkiestrator approve/reject` command
strings (verified with a headless `claude -p` run); they do not and cannot block equivalent Python run
through `python3 -c` or any other interpreter invocation. This is a known, accepted gap: closing it would
mean the gate authenticating its callers cryptographically (e.g. a capability token minted only inside the
CLI/panel processes), which is more machinery than this prototype carries. Treat "an agent has unrestricted
code execution in this environment" as already game over, the same as direct file access to `queue.db`.

## The owner panel

- Binds to `127.0.0.1` by default. `ORKI_HOST` can expose it on the network, but `serve()` then refuses to
  start unless `ORKI_TOKEN` is set explicitly (a random token would otherwise land in `docker logs`).
- The session cookie is `HttpOnly`, `SameSite=Strict`, and compared with `hmac.compare_digest`.
- `POST` requests are rejected if `Origin` doesn't match `Host` (CSRF).
- **The token passed as `?token=` in the URL is also the session secret.** It survives in browser history
  and in any proxy or server access log that records full URLs. Treat the link as a credential: don't
  paste it into chat, a public channel, or a URL shortener. Rotating it means restarting the process with
  a new `ORKI_TOKEN` (or letting it generate a new random one). **It is already exchanged for the session
  cookie on the first request and never kept in the address bar**: a `GET /?token=<valid>` sets the
  `HttpOnly` cookie and 303-redirects to the bare `/`, so the token appears in browser/proxy history only
  for that one, one-time request, not for the rest of the session (`tests/test_web.py::test_token_link_sets_session`,
  `tests/test_hitl.py::test_token_in_query_string_does_not_approve`). A wrong token gets 401, not a cookie.
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

- SQLite is a single file; writer serialization comes from SQLite's own locking, not from any
  application-level locking this project adds. This is a single-owner tool, not a multi-tenant system.
- A post stuck in `publishing` after a crash needs manual recovery (see "Publishing" above) — there is no
  automatic reconciliation against the real platform to find out whether it actually went out.
- A forged `human:` actor passed directly to `gate.transition()` by code that already has access to the
  `orkiestrator` package is not caught by `gate.py` — see "Publishing" above for why, and why it isn't fixed.
- `data/` (the database, backups, outbox) is not encrypted at rest.
- There is no rate limiting on the panel beyond the OS/network layer.
