"""CLI. `approve` and `reject` are human-only: they need an interactive terminal confirmation."""
import argparse
import getpass
import sys

from . import attribution, brief, gate, leads, publisher
from .db import ROOT, connect


def _human():
    return "human:" + getpass.getuser()


def _confirm(prompt):
    if not sys.stdin.isatty():
        print("Refusing: approval needs an interactive terminal (the owner), not a script or agent.", file=sys.stderr)
        return False
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def main(argv=None):
    p = argparse.ArgumentParser(prog="orkiestrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="agent: add a draft")
    a.add_argument("--agent", required=True)
    a.add_argument("--platform", required=True)
    a.add_argument("--kind", required=True)
    a.add_argument("--hook", default="")
    a.add_argument("--title", default="")
    a.add_argument("--brief", default="")
    a.add_argument("--link-id", default="")
    a.add_argument("--body", required=True)

    for name in ("submit", "show"):
        s = sub.add_parser(name)
        s.add_argument("id", type=int)
        s.add_argument("--agent", default="orchestrator")
    ap = sub.add_parser("approve", help="owner only")
    ap.add_argument("id", type=int)
    rj = sub.add_parser("reject", help="owner only")
    rj.add_argument("id", type=int)
    rj.add_argument("--note", default="")
    sc = sub.add_parser("schedule")
    sc.add_argument("id", type=int)
    sc.add_argument("--at", required=True, help="ISO time, e.g. 2026-10-20T09:00:00+00:00")
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    sub.add_parser("publish-due", help="publish approved posts whose time has come")
    ld = sub.add_parser("lead", help="classify an inbound message and capture buying signals")
    ld.add_argument("--platform", required=True)
    ld.add_argument("--handle", required=True)
    ld.add_argument("--message", required=True)
    mi = sub.add_parser("import-metrics")
    mi.add_argument("csv")
    sv = sub.add_parser("serve", help="start the web panel (owner)")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-browser", action="store_true")
    br = sub.add_parser("brief")
    br.add_argument("--write", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        from . import web
        web.serve(args.port, not args.no_browser)
        return 0
    conn = connect()
    try:
        if args.cmd == "add":
            pid = gate.add_draft(conn, f"agent:{args.agent}", args.platform, args.kind, args.body, args.title, args.hook, args.brief, args.link_id)
            print(f"draft #{pid} created")
        elif args.cmd == "submit":
            gate.transition(conn, args.id, "submit", f"agent:{args.agent}")
            print(f"#{args.id} pending approval")
        elif args.cmd == "show":
            r = conn.execute("SELECT * FROM posts WHERE id=?", (args.id,)).fetchone()
            print("not found" if r is None else "\n".join(f"{k}: {r[k]}" for k in r.keys()))
        elif args.cmd == "approve":
            r = conn.execute("SELECT platform, hook, body FROM posts WHERE id=?", (args.id,)).fetchone()
            if r is None:
                print("not found", file=sys.stderr)
                return 1
            print(f"[{r['platform']}] {r['hook']}\n{r['body']}\n")
            if not _confirm(f"Approve #{args.id} for publishing?"):
                return 1
            gate.transition(conn, args.id, "approve", _human())
            print(f"#{args.id} approved")
        elif args.cmd == "reject":
            if not _confirm(f"Reject #{args.id}?"):
                return 1
            gate.transition(conn, args.id, "reject", _human(), note=args.note)
            print(f"#{args.id} rejected")
        elif args.cmd == "schedule":
            gate.transition(conn, args.id, "schedule", "agent:scheduler", scheduled_at=args.at)
            print(f"#{args.id} scheduled for {args.at}")
        elif args.cmd == "list":
            q, params = "SELECT id, platform, kind, status, hook FROM posts", ()
            if args.status:
                q, params = q + " WHERE status=?", (args.status,)
            for r in conn.execute(q + " ORDER BY id", params):
                print(f"#{r['id']} {r['status']:<17} {r['platform']}/{r['kind']}  {r['hook']}")
        elif args.cmd == "publish-due":
            for pid, ref in publisher.run_due(conn):
                print(f"published #{pid} -> {ref}")
        elif args.cmd == "lead":
            print(leads.capture(conn, args.platform, args.handle, args.message))
        elif args.cmd == "import-metrics":
            attribution.import_csv(conn, args.csv)
            print("metrics imported")
        elif args.cmd == "brief":
            text = brief.render(conn)
            if args.write:
                out = ROOT / "data" / "reports"
                out.mkdir(parents=True, exist_ok=True)
                from datetime import date
                path = out / f"{date.today().isoformat()}.md"
                path.write_text(text, encoding="utf-8")
                print(path)
            else:
                print(text)
    except gate.GateError as e:
        print(f"blocked: {e}", file=sys.stderr)
        return 2
    return 0
