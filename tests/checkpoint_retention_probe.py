#!/usr/bin/env python3
"""Checkpoint retention probe (feat/checkpoint-retention, iss #11).

Run inside the official image with the plugin installed (see the other
probes' container recipe). The unit part needs no server: it fabricates
snapshot sets with controlled mtimes and asserts the policy directly
against _prune_checkpoint_artifacts. The integration part (runs when
MEMORYWEB_BIN is set) drives a real on_pre_compress against a demo
database and asserts the commit lands AND the directory trims to the
floor.

Cases:
  A. 6 fresh snapshots -> 5 kept (count cap, the order's "6 leave 5")
  B. 5 fresh + 4 expired -> the 4 expired are cleaned, 5 fresh stay
  C. 4 all-expired -> all 4 stay (recovery floor holds even past the age window)
  D. 2 fresh + 5 expired -> 5 stay (floor rescues the NEWEST 3 expired)
  E. staging strays: hour-old stray swept, 5-minute stray kept (in-flight grace)
  F. integration: 6 pre-existing fresh + on_pre_compress -> exactly 5,
     newest is the just-committed one, oldest pre-existing gone
"""
import os
import sys
import time
from pathlib import Path

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def snap(checkpoint_dir: Path, name: str, age_days: float = 0) -> Path:
    p = checkpoint_dir / name
    p.write_bytes(b"snapshot")
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def stray(db_dir: Path, name: str, age_seconds: float) -> Path:
    p = db_dir / name
    p.write_bytes(b"staging leftover")
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


def main() -> int:
    sys.path.insert(0, "/opt/hermes")
    sys.path.insert(0, os.path.join(os.environ.get("HERMES_HOME", ""), "plugins"))
    import memoryweb as mw

    prune = mw._prune_checkpoint_artifacts
    keep = mw._CHECKPOINT_KEEP

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        db_dir = Path(td)
        ckdir = db_dir / "memoryweb-checkpoints"
        ckdir.mkdir()

        print("== A: 6 fresh -> 5 ==")
        for i in range(6):
            snap(ckdir, f"pre-compress-a{i}.db", age_days=0.1 * i)
        r = prune(db_dir)
        left = sorted(p.name for p in ckdir.iterdir())
        check("6 fresh keep 5", len(left) == 5, f"left={left}")
        check("the oldest beyond the floor is the one removed", "pre-compress-a5.db" not in left)

        print("== B: 5 fresh + 4 expired -> expired cleaned ==")
        for p in ckdir.iterdir():
            p.unlink()
        for i in range(5):
            snap(ckdir, f"pre-compress-b-f{i}.db", age_days=i)
        for i in range(4):
            snap(ckdir, f"pre-compress-b-x{i}.db", age_days=20 + i)
        r = prune(db_dir)
        left = sorted(p.name for p in ckdir.iterdir())
        check("expired beyond the floor cleaned", all("b-x" not in n for n in left), f"left={left}")
        check("5 fresh stay", len([n for n in left if "b-f" in n]) == 5)

        print("== C: 4 all-expired -> floor holds ==")
        ckdir2 = db_dir / "memoryweb-checkpoints"
        for p in ckdir2.iterdir():
            p.unlink()
        for i in range(4):
            snap(ckdir2, f"pre-compress-c{i}.db", age_days=30 + i)
        prune(db_dir)
        left = sorted(p.name for p in ckdir2.iterdir())
        check("all-expired set keeps its 4 (floor = min(5, total))", len(left) == 4, f"left={left}")

        print("== D: 2 fresh + 5 expired -> floor rescues NEWEST expired ==")
        for p in ckdir2.iterdir():
            p.unlink()
        for i in range(2):
            snap(ckdir2, f"pre-compress-d-f{i}.db", age_days=i)
        for i in range(5):
            snap(ckdir2, f"pre-compress-d-x{i}.db", age_days=20 + i)  # x0 newest expired
        prune(db_dir)
        left = sorted(p.name for p in ckdir2.iterdir())
        check("total stays at the floor (5)", len(left) == 5, f"left={left}")
        check("the NEWEST expired are the rescued ones",
              all(f"pre-compress-d-x{i}.db" in left for i in range(3)),
              f"left={left}")

        print("== E: staging strays ==")
        for p in ckdir2.iterdir():
            p.unlink()
        snap(ckdir2, "pre-compress-e0.db")
        old_stray = stray(db_dir, ".memoryweb-checkpoint-stale-123.db", 7200)
        new_stray = stray(db_dir, ".memoryweb-checkpoint-inflight-456.db", 300)
        r = prune(db_dir)
        check("hour-old staging stray swept", not old_stray.exists())
        check("5-minute staging stray kept (in-flight grace)", new_stray.exists())
        check("checkpoint dir untouched by the stray sweep", any(ckdir2.iterdir()))

    # -- integration: real on_pre_compress -------------------------------------
    bin_path = os.environ.get("MEMORYWEB_BIN")
    db_path = os.environ.get("MEMORYWEB_DB")
    if bin_path and db_path:
        print("== F: integration — on_pre_compress commits and trims ==")
        from plugins.memory import load_memory_provider

        db_dir = Path(db_path).expanduser().resolve().parent
        ckdir = db_dir / "memoryweb-checkpoints"
        ckdir.mkdir(parents=True, exist_ok=True)
        for p in ckdir.iterdir():
            p.unlink()
        for i in range(6):
            snap(ckdir, f"pre-compress-f{i}.db", age_days=0.1 * i)

        p = load_memory_provider("memoryweb")
        p.initialize("retention-probe", hermes_home=os.environ.get("HERMES_HOME", "/opt/data"),
                     platform="verify", agent_context="primary")
        msg = p.on_pre_compress([{"role": "user", "content": "probe"}])
        p.shutdown()

        left = sorted((q.name, q.stat().st_mtime) for q in ckdir.iterdir())
        names = sorted(n for n, _ in left)
        check("snapshot committed", "committed" in msg)
        check("directory trimmed to the floor after the commit", len(left) == keep, f"names={names}")
        newest = max(left, key=lambda t: t[1])[0]
        check("the just-committed snapshot is the newest (not a fabricated one)",
              newest.startswith("pre-compress-") and "-f" not in newest, f"newest={newest}")
        check("oldest pre-existing snapshots rotated out",
              not any(f"pre-compress-f{i}.db" in names for i in (4, 5)))

    print("CHECKPOINT RETENTION PROBE " + ("GREEN" if not FAILURES else f"RED ({len(FAILURES)} failed)"))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
