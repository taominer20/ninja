#!/usr/bin/env python3.13
"""
Overnight A/B validator for held-back toolbox tweaks.

Strategy:
  1. Build BASELINE variant once: Mine016 + T1-T4 (current active toolbox).
  2. Run BASELINE on N tasks -> scores B[t].
  3. For each held-back candidate (T5, T6, T8, T9, T10):
       Build CANDIDATE = baseline + this tweak.
       Run CANDIDATE on the same N tasks -> scores C[t].
       avg_delta = mean(C) - mean(B).
       If avg_delta >= +0.03: PROMOTE — append to TWEAKS list in
       apply_toolbox.py, git commit + push.
  4. Final summary written to /tmp/validate_overnight.summary.

T7 (gemini_self_review) is skipped — its hook is gated by NINJA_SELF_REVIEW=1
which must be threaded through tau's docker exec env. Validate separately.

Tasks (known Mine016-tractable from prior benches):
  t1, t11, t14, t15, t16

Estimated wall time on this Mac: ~90 min for 30 solves @ ~3 min each.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NINJA_ROOT = Path(__file__).resolve().parent
TAU = Path("/Users/alexanderlange/tau-fast")

sys.path.insert(0, str(NINJA_ROOT))
from apply_toolbox import (
    t1_task_style_off, t2_file_targeting, t3_blank_run_collapse,
    t4_edge_g_ensure,
    t5_minimal_change_dir, t6_pre_plan_turn,
    t8_style_mirror_dir, t9_reread_task_dir, t10_hallucinated_suppress,
)

# Mine016 is the current king SHA (as of 2026-04-25).
KING_REPO = "https://github.com/VladaWebDev/Mine016.git"
KING_SHA = "b6a99c4296b01901f47b0dba2d95f68b9831a32d"
TASKS = ["t1", "t11", "t14", "t15", "t16"]

BASELINE_TWEAKS = [
    ("T1", t1_task_style_off),
    ("T4", t4_edge_g_ensure),
    ("T2", t2_file_targeting),
    ("T3", t3_blank_run_collapse),
]

CANDIDATES = [
    ("T5", "minimal_change_dir", t5_minimal_change_dir),
    ("T6", "pre_plan_turn", t6_pre_plan_turn),
    ("T8", "style_mirror_dir", t8_style_mirror_dir),
    ("T9", "reread_task_dir", t9_reread_task_dir),
    ("T10", "hallucinated_suppress", t10_hallucinated_suppress),
]

LOG = NINJA_ROOT / ".validate_overnight.log"
SUMMARY = Path("/tmp/validate_overnight.summary")
SCORE_HELPER = Path("/tmp/score_vs_ref.py")
PROMOTION_THRESHOLD = 0.03  # candidate avg score must beat baseline by this
TIMEOUT_SOLVE = 360
SOLVER_MAX_COST = 0.30
AGENT_TIMEOUT = 200


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cleanup_docker():
    run(["bash", "-c", "docker rm -f $(docker ps -aq --filter name=swe-eval) >/dev/null 2>&1; true"])


def make_variant(dest: Path, extra_tweaks: list) -> str | None:
    """Clone king at fixed SHA, apply baseline + extras, commit. Return new SHA."""
    if dest.exists():
        shutil.rmtree(dest)
    r = run(["git", "clone", "--depth=20", KING_REPO, str(dest)])
    if r.returncode != 0:
        log(f"clone failed: {r.stderr.strip()[:200]}")
        return None
    run(["git", "-C", str(dest), "fetch", "--depth=1", "origin", KING_SHA])
    r = run(["git", "-C", str(dest), "checkout", KING_SHA])
    if r.returncode != 0:
        log(f"checkout failed: {r.stderr.strip()[:200]}")
        return None
    applied = []
    skipped = []
    for label, fn in BASELINE_TWEAKS + [(name, fn) for name, _, fn in extra_tweaks]:
        try:
            res = fn(dest)
            (applied if res.applied else skipped).append((label, res.notes or res.skipped_reason))
        except Exception as e:
            log(f"tweak {label} raised {e}")
            skipped.append((label, f"exception: {e}"))
    log(f"  applied: {[a[0] for a in applied]}; skipped: {[s[0] for s in skipped]}")
    run(["git", "-C", str(dest), "config", "user.email", "validate@local"])
    run(["git", "-C", str(dest), "config", "user.name", "validator"])
    run(["git", "-C", str(dest), "add", "-A"])
    run(["git", "-C", str(dest), "commit", "-m", "validate"])
    sha_r = run(["git", "-C", str(dest), "rev-parse", "HEAD"])
    return sha_r.stdout.strip() if sha_r.returncode == 0 else None


def solve(task: str, sol: str, agent: Path) -> bool:
    """Run tau solve for one task/agent. Returns True on success."""
    sol_dir = TAU / "workspace" / "tasks" / task / "solutions" / sol
    if sol_dir.exists():
        shutil.rmtree(sol_dir)
    cleanup_docker()
    cmd = [
        "timeout", str(TIMEOUT_SOLVE), "uv", "run", "tau", "solve",
        "--task", task, "--solution", sol, "--agent", str(agent),
        "--solver-max-cost", str(SOLVER_MAX_COST),
        "--agent-timeout", str(AGENT_TIMEOUT),
        "--seed", "42",
    ]
    r = run(cmd, cwd=TAU)
    cleanup_docker()
    return r.returncode == 0


def score(task: str, sol: str) -> float | None:
    task_root = TAU / "workspace" / "tasks" / task
    r = run(["uv", "run", "python", str(SCORE_HELPER), str(task_root), sol], cwd=TAU)
    if r.returncode != 0:
        return None
    line = (r.stdout or "").splitlines()[0] if r.stdout else ""
    try:
        return float(line.split()[0])
    except Exception:
        return None


def bench_variant(label: str, agent: Path) -> dict[str, float | None]:
    """Run all TASKS through one variant. Return {task: score_or_None}."""
    out: dict[str, float | None] = {}
    for t in TASKS:
        sol = f"val_{label}"
        log(f"  solve {label} on {t} ...")
        ok = solve(t, sol, agent)
        s = score(t, sol) if ok else None
        log(f"    {t} -> {s}")
        out[t] = s
    return out


def avg(scores: dict[str, float | None]) -> float:
    vals = [v for v in scores.values() if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def promote(name: str, label: str) -> bool:
    """Append the candidate's TWEAKS entry to apply_toolbox.py and commit + push."""
    f = NINJA_ROOT / "apply_toolbox.py"
    content = f.read_text()
    marker = "    # Held back pending individual validation:"
    if marker not in content:
        log(f"PROMOTE {name}: marker not found — skipping auto-edit")
        return False
    func_name = f"t{name[1:]}_{label}"  # T5 -> t5_minimal_change_dir
    new_line = f'    ("{name} {label}", {func_name}),  # promoted by validate_overnight'
    if new_line in content:
        log(f"PROMOTE {name}: entry already in TWEAKS — no-op")
        return True
    new_content = content.replace(marker, new_line + "\n" + marker, 1)
    f.write_text(new_content)
    run(["git", "-C", str(NINJA_ROOT), "add", "apply_toolbox.py"])
    msg = f"toolbox: promote {name} {label} (validated +{PROMOTION_THRESHOLD*100:.0f}%+ avg uplift on {len(TASKS)} tasks)"
    r = run(["git", "-C", str(NINJA_ROOT), "commit", "-m", msg])
    if r.returncode != 0:
        log(f"PROMOTE {name}: commit failed: {r.stderr.strip()[:200]}")
        return False
    rp = run(["git", "-C", str(NINJA_ROOT), "push", "origin", "main"])
    if rp.returncode != 0:
        log(f"PROMOTE {name}: push failed: {rp.stderr.strip()[:200]}")
        return False
    log(f"PROMOTE {name}: committed + pushed")
    return True


def write_summary(baseline: dict, results: list[dict]) -> None:
    lines = []
    lines.append("=== Overnight validation summary ===")
    lines.append(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"tasks: {TASKS}")
    lines.append(f"king: {KING_SHA[:8]}")
    lines.append("")
    lines.append("Baseline (T1-T4) per-task:")
    for t in TASKS:
        lines.append(f"  {t}: {baseline.get(t)}")
    lines.append(f"Baseline avg: {avg(baseline):.4f}")
    lines.append("")
    for r in results:
        lines.append(f"--- {r['name']} {r['label']} ---")
        for t in TASKS:
            lines.append(f"  {t}: candidate={r['scores'].get(t)} (baseline={baseline.get(t)})")
        delta = r["candidate_avg"] - r["baseline_avg"]
        verdict = "PROMOTE" if delta >= PROMOTION_THRESHOLD else "hold"
        lines.append(f"  candidate avg: {r['candidate_avg']:.4f}  delta vs baseline: {delta:+.4f}  -> {verdict}")
        lines.append("")
    SUMMARY.write_text("\n".join(lines))


def main() -> int:
    log("=== validate_overnight start ===")
    log(f"tasks: {TASKS}")
    log(f"candidates: {[c[0] for c in CANDIDATES]}")

    baseline_dir = Path("/tmp/val_baseline")
    bsha = make_variant(baseline_dir, [])
    if bsha is None:
        log("baseline build failed — aborting")
        return 1
    log(f"baseline @ {bsha[:12]}")

    log("=== bench BASELINE ===")
    baseline = bench_variant("baseline", baseline_dir)
    base_avg = avg(baseline)
    log(f"baseline avg: {base_avg:.4f}")

    results = []
    for name, label, fn in CANDIDATES:
        log(f"=== candidate {name} {label} ===")
        cdir = Path(f"/tmp/val_{name.lower()}")
        csha = make_variant(cdir, [(name, label, fn)])
        if csha is None:
            log(f"{name} build failed — skipping")
            continue
        log(f"  candidate @ {csha[:12]}")
        cscores = bench_variant(name.lower(), cdir)
        c_avg = avg(cscores)
        delta = c_avg - base_avg
        log(f"  {name} avg: {c_avg:.4f}  delta: {delta:+.4f}")
        results.append({
            "name": name, "label": label,
            "scores": cscores, "candidate_avg": c_avg,
            "baseline_avg": base_avg,
        })
        if delta >= PROMOTION_THRESHOLD:
            log(f"{name} PROMOTE candidate")
            promote(name, label)
        else:
            log(f"{name} hold")

    write_summary(baseline, results)
    log("=== validate_overnight done ===")
    log(f"summary: {SUMMARY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
