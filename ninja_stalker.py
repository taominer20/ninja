#!/usr/bin/env python3.13
"""
Ninja stalker — autonomous "copy the new king" agent.

Polls https://ninja.arbos.life/dashboard.json. Whenever a duel completes
with `king_replaced=true` (challenger actually dethroned the prior king),
the agent clones the new king's repo at their on-chain SHA, pushes a
tracking branch to our own GitHub, and updates ALL of our hotkeys' on-
chain commitments to point at that SHA. Triggering on king dethrone-
ments is strictly stronger than chasing close losses: the new king has a
proven >55% win rate against the very repo we'd otherwise be losing to.

Dethrone rule reference (from the dashboard's scoring config):
    decisive = wins + losses                    # ties ignored
    needed   = (decisive // 2) + 6              # majority + win_margin (5)

Hotkey strategy:
    On every dethrone event, push the new king's SHA to OUR repo, then
    commit `taominer20/ninja@<sha>` to ALL hotkeys in HOTKEYS at once.
    Reasoning: only the latest king is the latest king; spreading our
    hotkeys across older kings leaves slots stale.

Resilience to challenger repo deletion:
    The on-chain commitment points at OUR repo. We push the new king's
    commit onto a uniquely-named branch in our repo BEFORE committing
    on-chain. Even if the original miner deletes their repo seconds
    later, the validator's `git fetch --depth=1 origin <sha>` against
    OUR repo still resolves the SHA from our stalk-* branch.

Idempotent: state file stores seen duel_ids and per-hotkey timestamps.

Run continuously:
    python3.13 ninja_stalker.py
or via cron every 10 minutes:
    */10 * * * * cd /Users/alexanderlange/Desktop/ninja && python3.13 ninja_stalker.py --once
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
STATE_FILE = REPO_ROOT / ".stalker_state.json"
LOG_FILE = REPO_ROOT / ".stalker.log"
DASHBOARD_URL = "https://ninja.arbos.life/dashboard.json"
TARGET_REPO = os.environ.get("STALKER_TARGET_REPO", "taominer20/ninja")
HOTKEYS = os.environ.get("STALKER_HOTKEYS", "hotkey_v17,hotkey_v18,hotkey_v19,hotkey_v20").split(",")
WALLET = os.environ.get("STALKER_WALLET", "miner")
NETUID = int(os.environ.get("STALKER_NETUID", "66"))
NETWORK = os.environ.get("STALKER_NETWORK", "finney")

POLL_INTERVAL_SEC = int(os.environ.get("STALKER_POLL_SEC", "300"))
HOTKEY_COOLDOWN_SEC = int(os.environ.get("STALKER_COOLDOWN_SEC", "60"))
MIN_DECISIVE_ROUNDS = int(os.environ.get("STALKER_MIN_DECISIVE", "10"))


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log("state file corrupt; starting fresh")
    return {"seen_duels": [], "hotkey_index": 0, "hotkey_last_commit": {}, "commits": []}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def fetch_dashboard() -> dict:
    req = urllib.request.Request(DASHBOARD_URL, headers={"User-Agent": "Mozilla/5.0 ninja-stalker"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def needed_to_dethrone(wins: int, losses: int) -> int:
    """Match the user-specified rule exactly: ties dropped, majority + 5."""
    decisive = wins + losses
    return (decisive // 2) + 6


def find_dethrone_events(duels: list[dict], seen_ids: set[int]) -> list[dict]:
    """Return duels where the challenger dethroned the king (king_replaced=true)."""
    out = []
    for d in duels:
        duel_id = d.get("duel_id")
        if duel_id in seen_ids:
            continue
        if d.get("disqualification_reason"):
            continue
        if not d.get("king_replaced"):
            continue
        wins = int(d.get("wins") or 0)
        losses = int(d.get("losses") or 0)
        decisive = wins + losses
        if decisive < MIN_DECISIVE_ROUNDS:
            continue
        needed = needed_to_dethrone(wins, losses)
        out.append({
            "duel_id": duel_id,
            "finished_at": d.get("finished_at"),
            "challenger_repo": d.get("challenger_repo"),
            "challenger_repo_url": d.get("challenger_repo_url"),
            "challenger_commit_sha": d.get("challenger_commit_sha"),
            "challenger_uid": d.get("challenger_uid"),
            "wins": wins,
            "losses": losses,
            "ties": d.get("ties") or 0,
            "needed": needed,
            "margin": wins - needed,
        })
    out.sort(key=lambda x: x.get("finished_at") or "")
    return out


def hotkeys_off_cooldown(state: dict) -> list[str]:
    """Return all hotkeys whose last commit was longer ago than the cooldown."""
    now = time.time()
    out = []
    for hk in HOTKEYS:
        last = float(state.get("hotkey_last_commit", {}).get(hk) or 0.0)
        if now - last >= HOTKEY_COOLDOWN_SEC:
            out.append(hk)
    return out


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=False, capture_output=True, text=True, **kw)


def fork_apply_push(
    repo_full_name: str, commit_sha: str, target_repo: str, apply_toolbox: bool = True
) -> tuple[bool, str, str]:
    """Clone repo@sha, apply our toolbox of validated tweaks, commit on top,
    push to our repo as a tracking branch. Returns (ok, branch_or_err, new_sha)."""
    workdir = Path("/tmp") / f"stalk_{commit_sha[:12]}"
    if workdir.exists():
        shutil.rmtree(workdir)
    src_url = f"https://github.com/{repo_full_name}.git"
    branch = f"fork-{repo_full_name.replace('/', '-')}-{commit_sha[:8]}"
    r = run(["git", "clone", "--no-tags", src_url, str(workdir)])
    if r.returncode != 0:
        return False, f"clone failed: {r.stderr.strip()[:300]}", ""
    run(["git", "-C", str(workdir), "fetch", "--depth=1", "origin", commit_sha])
    rc = run(["git", "-C", str(workdir), "checkout", commit_sha])
    if rc.returncode != 0:
        return False, f"checkout {commit_sha[:8]} failed: {rc.stderr.strip()[:300]}", ""
    if not (workdir / "agent").is_dir():
        return False, "missing agent/ directory at this SHA", ""
    pkg_json = workdir / "agent" / "package.json"
    if not pkg_json.is_file():
        return False, "missing agent/package.json at this SHA", ""
    rb = run(["git", "-C", str(workdir), "checkout", "-B", branch])
    if rb.returncode != 0:
        return False, f"branch failed: {rb.stderr.strip()[:300]}", ""

    new_sha = commit_sha
    if apply_toolbox:
        try:
            from apply_toolbox import apply_all
            summary = apply_all(workdir)
            applied = [t for t in summary["tweaks"] if t["applied"]]
            log(f"toolbox: applied {len(applied)}/{len(summary['tweaks'])} tweaks "
                f"({', '.join(t['tweak'] for t in applied)})")
            if applied:
                run(["git", "-C", str(workdir), "config", "user.email", "stalker@taominer20.local"])
                run(["git", "-C", str(workdir), "config", "user.name", "ninja-stalker"])
                run(["git", "-C", str(workdir), "add", "-A"])
                names = ", ".join(t["tweak"] for t in applied)
                msg = f"toolbox: apply {len(applied)} tweaks ({names})"
                rcm = run(["git", "-C", str(workdir), "commit", "-m", msg])
                if rcm.returncode == 0:
                    rs = run(["git", "-C", str(workdir), "rev-parse", "HEAD"])
                    if rs.returncode == 0:
                        new_sha = rs.stdout.strip()
                        log(f"toolbox commit landed: {new_sha[:12]}")
        except Exception as e:
            log(f"toolbox apply failed (continuing with vanilla fork): {e}")

    dest_url = f"https://github.com/{target_repo}.git"
    rs = run(["git", "-C", str(workdir), "remote", "set-url", "origin", dest_url])
    if rs.returncode != 0:
        return False, f"set-url failed: {rs.stderr.strip()[:200]}", ""
    rp = run(["git", "-C", str(workdir), "push", "origin", branch, "--force"])
    if rp.returncode != 0:
        return False, f"push failed: {rp.stderr.strip()[:300]}", ""
    return True, branch, new_sha


def clone_and_push(repo_full_name: str, commit_sha: str, target_repo: str) -> tuple[bool, str]:
    """Backwards-compat wrapper used in older code paths. Use fork_apply_push for new."""
    ok, info, _ = fork_apply_push(repo_full_name, commit_sha, target_repo, apply_toolbox=False)
    return ok, info


def register_on_chain(hotkey: str, repo: str, sha: str) -> tuple[bool, str]:
    env = os.environ.copy()
    env["NINJA_WALLET"] = WALLET
    env["NINJA_HOTKEY"] = hotkey
    env["NINJA_NETUID"] = str(NETUID)
    env["NINJA_REPO"] = repo
    env["NINJA_SHA"] = sha
    env["NINJA_NETWORK"] = NETWORK
    r = run(["python3.13", str(REPO_ROOT / "commit_agent.py")], env=env)
    if r.returncode != 0:
        return False, f"commit_agent rc={r.returncode}: {r.stderr.strip()[:400] or r.stdout.strip()[:400]}"
    return True, r.stdout.strip()[-300:]


def process_dethrone(duel: dict, state: dict) -> None:
    sha = duel["challenger_commit_sha"]
    repo = duel["challenger_repo"]
    if not sha or not repo:
        log(f"skip duel {duel['duel_id']} — missing sha/repo")
        return
    eligible = hotkeys_off_cooldown(state)
    if not eligible:
        log(f"all hotkeys on cooldown; deferring duel {duel['duel_id']}")
        return
    log(f"duel {duel['duel_id']} NEW KING {repo}@{sha[:8]} W/L={duel['wins']}/{duel['losses']} → fork+toolbox then commit under {eligible}")
    apply_tb = os.environ.get("STALKER_APPLY_TOOLBOX", "1") != "0"
    ok, info, new_sha = fork_apply_push(repo, sha, TARGET_REPO, apply_toolbox=apply_tb)
    if not ok:
        log(f"clone/fork failed for duel {duel['duel_id']}: {info}")
        state.setdefault("seen_duels", []).append(duel["duel_id"])
        save_state(state)
        return
    if new_sha != sha:
        log(f"forked → {TARGET_REPO}@{new_sha[:12]} (toolbox added on top of {sha[:8]})")
    else:
        log(f"forked → {TARGET_REPO}@{new_sha[:12]} (no toolbox tweaks needed)")
    push_sha = new_sha
    successes = []
    failures = []
    now = time.time()
    for hk in eligible:
        ok, info = register_on_chain(hk, TARGET_REPO, push_sha)
        if ok:
            log(f"registered {TARGET_REPO}@{push_sha[:12]} under {hk}")
            state.setdefault("hotkey_last_commit", {})[hk] = now
            state.setdefault("commits", []).append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "hotkey": hk,
                "repo": TARGET_REPO,
                "sha": push_sha,
                "source_repo": repo,
                "source_sha": sha,
                "duel_id": duel["duel_id"],
                "wins": duel["wins"],
                "losses": duel["losses"],
            })
            successes.append(hk)
        else:
            log(f"on-chain commit failed for {hk}: {info}")
            failures.append(hk)
        save_state(state)
    log(f"duel {duel['duel_id']} done: success={successes} fail={failures}")
    state.setdefault("seen_duels", []).append(duel["duel_id"])
    save_state(state)


def tick() -> None:
    state = load_state()
    seen = set(state.get("seen_duels", []))
    try:
        dash = fetch_dashboard()
    except Exception as e:
        log(f"fetch dashboard failed: {e}")
        return
    events = find_dethrone_events(dash.get("duels", []), seen)
    if not events:
        log(f"no new king dethronings (seen={len(seen)})")
        return
    log(f"found {len(events)} new king dethroning event(s)")
    # Only act on the latest dethroning — older ones have been dethroned already
    # by definition (a king is only king once). Mark older ones seen without
    # acting; act on the freshest one.
    for older in events[:-1]:
        state.setdefault("seen_duels", []).append(older["duel_id"])
        log(f"skipping superseded dethroning duel {older['duel_id']} ({older['challenger_repo']}@{older['challenger_commit_sha'][:8]})")
    save_state(state)
    process_dethrone(events[-1], state)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="single pass then exit (cron mode)")
    p.add_argument("--dry-run", action="store_true", help="report close-misses without cloning or committing")
    p.add_argument("--show-recent", type=int, default=0, help="print N most recent close-misses found in dashboard and exit")
    p.add_argument("--init", action="store_true", help="mark all current dashboard duel_ids as seen and exit; run before first daemon start to skip historical close-misses")
    p.add_argument("--status", action="store_true", help="print state summary and exit")
    args = p.parse_args()

    if args.status:
        state = load_state()
        seen = state.get("seen_duels", [])
        commits = state.get("commits", [])
        print(f"seen_duels: {len(seen)}")
        print(f"hotkey_index: {state.get('hotkey_index', 0)}")
        print(f"hotkey_last_commit:")
        for hk, ts in (state.get("hotkey_last_commit") or {}).items():
            ago = time.time() - float(ts)
            print(f"  {hk}: {datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()} ({ago/60:.0f} min ago)")
        print(f"committed actions: {len(commits)}")
        for c in commits[-10:]:
            print(f"  {c['ts']} {c['hotkey']} ← {c['repo']}@{c['sha'][:8]} (duel {c['duel_id']}, margin={c['margin']})")
        return 0

    if args.init:
        state = load_state()
        dash = fetch_dashboard()
        all_ids = sorted({d.get("duel_id") for d in dash.get("duels", []) if d.get("duel_id") is not None})
        state["seen_duels"] = all_ids
        save_state(state)
        log(f"init: marked {len(all_ids)} historical duel_ids as seen")
        return 0

    if args.show_recent > 0:
        dash = fetch_dashboard()
        events = find_dethrone_events(dash.get("duels", []), seen_ids=set())
        for e in events[-args.show_recent:]:
            print(json.dumps(e, indent=2))
        return 0

    if args.dry_run:
        state = load_state()
        seen = set(state.get("seen_duels", []))
        dash = fetch_dashboard()
        events = find_dethrone_events(dash.get("duels", []), seen)
        log(f"[dry-run] {len(events)} unseen king dethroning(s); would act on the LATEST only")
        for e in events:
            tag = "ACT" if e is events[-1] else "skip"
            log(f"  [{tag}] duel {e['duel_id']} {e['challenger_repo']}@{e['challenger_commit_sha'][:8]} W/L={e['wins']}/{e['losses']} finished={e['finished_at']}")
        return 0

    if args.once:
        tick()
        return 0

    log(f"stalker start — poll {POLL_INTERVAL_SEC}s, hotkeys={HOTKEYS}, target={TARGET_REPO}")
    while True:
        try:
            tick()
        except KeyboardInterrupt:
            log("interrupted")
            return 0
        except Exception as e:
            log(f"unexpected error: {e}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
