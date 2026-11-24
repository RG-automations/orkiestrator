"""Owner-triggered removal of test data, limited to what a manifest lists.

The manifest (data/cleanup_manifest.json) names exact post/lead/inbound ids and exact paths. Nothing outside it
is touched, paths are confined to data/ and demo/, the queue database itself can never be listed, and a backup of
the database is written to data/backups/ before anything is deleted. Deleting only happens from the panel button.
"""
import json
import shutil
import sqlite3
from pathlib import Path

from .db import ROOT
from .gate import now

MANIFEST = ROOT / "data" / "cleanup_manifest.json"
ID_KEYS = (("post_ids", "posts"), ("lead_ids", "leads"), ("inbound_ids", "inbound"))


def load(path=None):
    p = Path(path or MANIFEST)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _target(rel, root):
    """Resolves a manifest path and refuses anything outside data/ and demo/, the data/ root itself, or the database."""
    base = Path(root).resolve()
    target = (base / rel).resolve()
    data, demo = base / "data", base / "demo"
    inside = data in target.parents or demo in target.parents or target == demo  # demo/ itself may go; data/ itself may not
    if target == data or not inside:
        raise ValueError(f"Niedozwolona ścieżka w manifeście: {rel}")
    if target.name.startswith("queue") or target.name == MANIFEST.name:
        raise ValueError(f"Tego pliku nie wolno usuwać: {rel}")
    return target


def preview(conn, manifest, root=None):
    """What would be removed right now (ids that no longer exist are ignored)."""
    root = root or ROOT
    found = {}
    for key, table in ID_KEYS:
        ids = [int(i) for i in manifest.get(key, [])]
        listed = {r[0] for r in conn.execute(
            f"SELECT id FROM {table} WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
        flagged = {r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE is_test=1")}  # rows marked as test data
        found[table] = sorted(listed | flagged)
    found["paths"] = [str(p.relative_to(Path(root).resolve())) for p in (_target(r, root) for r in manifest.get("paths", [])) if p.exists()]
    return found


def run(conn, manifest, root=None, manifest_path=None):
    """Backs up the database, deletes the listed rows and paths, then retires the manifest. Returns a count summary."""
    root = Path(root or ROOT)
    found = preview(conn, manifest, root)  # validates every path before anything is touched
    backups = root / "data" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"queue-before-cleanup-{now().replace(':', '-')}.db"
    dest = sqlite3.connect(str(backup))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    with conn:
        if found["posts"]:
            marks = ",".join("?" * len(found["posts"]))
            conn.execute(f"DELETE FROM events WHERE post_id IN ({marks})", found["posts"])
        for _, table in ID_KEYS:
            if found[table]:
                conn.execute(f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(found[table]))})", found[table])
    removed_paths = 0
    for rel in found["paths"]:
        t = _target(rel, root)
        shutil.rmtree(t) if t.is_dir() else t.unlink()
        removed_paths += 1
    mp = Path(manifest_path or MANIFEST)
    if mp.exists():
        mp.rename(mp.with_suffix(".done.json"))
    return {"posts": len(found["posts"]), "leads": len(found["leads"]), "inbound": len(found["inbound"]),
            "paths": removed_paths, "backup": str(backup.relative_to(root))}
