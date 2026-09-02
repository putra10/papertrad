"""Check that sync_to_git survives the two failures that silently killed
whole trading sessions:

  1. a stale checkout, where origin already moved on and both sides appended
     to trade_log.jsonl
  2. a HEAD left detached by a wedged merge/rebase from an earlier cycle

Run: python test_sync_git.py
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

SRC = Path(__file__).parent


def git(cwd, *args, check=True):
    r = subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(f"git {' '.join(args)} failed:\n{r.stderr}")
    return r.stdout.strip()


def line(n):
    return json.dumps({"type": "decisions", "n": n}) + "\n"


def make_clone(root, name, remote):
    """A checkout that looks like actions/checkout@v4 leaves behind."""
    d = root / name
    git(root, "clone", str(remote), name)
    git(d, "config", "user.email", "bot@example.com")
    git(d, "config", "user.name", "bot")
    return d


def sync(repo):
    """Run the real sync_to_git inside `repo`."""
    return subprocess.run(
        [sys.executable, "-c", "import paper_trader; paper_trader.sync_to_git()"],
        cwd=repo, capture_output=True, text=True,
        env={**os.environ, "AUTO_COMMIT": "1", "PYTHONUNBUFFERED": "1"},
    )


def main():
    root = Path(tempfile.mkdtemp(prefix="synctest-"))
    try:
        remote = root / "remote.git"
        git(root, "init", "--bare", "-b", "main", str(remote))

        # seed: the state files, plus the code sync_to_git actually shells out to
        seed = root / "seed"
        seed.mkdir()
        git(seed, "init", "-b", "main")
        git(seed, "config", "user.email", "bot@example.com")
        git(seed, "config", "user.name", "bot")
        for f in ("paper_trader.py", "build_dashboard.py", ".gitattributes"):
            shutil.copy(SRC / f, seed / f)
        (seed / "trade_log.jsonl").write_text(line(1) + line(2), encoding="utf-8")
        (seed / "baseline_state.json").write_text("{}", encoding="utf-8")
        (seed / "docs").mkdir()
        (seed / "docs" / "index.html").write_text("<p>seed</p>", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "seed")
        git(seed, "push", str(remote), "main")

        # our run checks out here, while origin is still at the seed commit
        ours = make_clone(root, "ours", remote)

        # meanwhile the previous session pushes a cycle of its own
        theirs = make_clone(root, "theirs", remote)
        (theirs / "trade_log.jsonl").write_text(
            line(1) + line(2) + line(3), encoding="utf-8")
        (theirs / "docs" / "index.html").write_text("<p>theirs</p>", encoding="utf-8")
        git(theirs, "commit", "-am", "their cycle")
        git(theirs, "push")

        # --- case 1: stale base, both sides appended -----------------------
        (ours / "trade_log.jsonl").write_text(
            line(1) + line(2) + line(4), encoding="utf-8")
        r = sync(ours)
        assert "warn" not in r.stdout, r.stdout + r.stderr

        check = make_clone(root, "check1", remote)
        got = [json.loads(x)["n"] for x in
               (check / "trade_log.jsonl").read_text(encoding="utf-8").splitlines() if x]
        # union merge keeps both sides; order is the dashboard's problem, not ours
        assert sorted(got) == [1, 2, 3, 4], f"lost events: {got}"

        # --- case 2: HEAD left detached by an earlier wedged cycle ----------
        git(ours, "checkout", "--detach", "HEAD")
        assert git(ours, "symbolic-ref", "-q", "HEAD", check=False) == ""
        (ours / "trade_log.jsonl").write_text(
            line(1) + line(2) + line(3) + line(4) + line(5), encoding="utf-8")
        r = sync(ours)
        assert "warn" not in r.stdout, r.stdout + r.stderr

        check = make_clone(root, "check2", remote)
        got = [json.loads(x)["n"] for x in
               (check / "trade_log.jsonl").read_text(encoding="utf-8").splitlines() if x]
        assert sorted(got) == [1, 2, 3, 4, 5], f"lost events after detached HEAD: {got}"

        print("test_sync_git OK")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
