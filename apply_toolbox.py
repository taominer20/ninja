#!/usr/bin/env python3.13
"""
Apply ninja's validated tweak toolbox to a forked agent repo.

Usage:
    python3.13 apply_toolbox.py <path-to-cloned-repo>
or:
    from apply_toolbox import apply_all
    summary = apply_all(Path("/tmp/cloned_fork"))

Each tweak is idempotent: re-running on an already-tweaked repo is a no-op.
Tweaks detect their own presence via stable markers and skip if present.

The toolbox is the empirical edge accumulated across 13 days of bench
work. Each entry below has a "Why" line summarising the validation that
got it in. Adding a new entry should follow the same rule: validated on
local bench OR validated head-to-head against a recent king before it
joins the lineup.

Tweak inventory (current):
  T1 task_style_off          — flip Grit "between-lines" default to "off"
  T2 file_targeting          — pre-loop file-targeting hook + module
  T3 blank_run_collapse      — post-edit cleanup of 3+ consecutive blanks
  T4 edge_g_ensure           — ensure post-edit whitespace-noise cleanup is present
  T5 minimal_change_dir      — loop-start directive forbidding cosmetic changes
  T6 pre_plan_turn           — loop-start directive forcing structured plan first
  T7 gemini_self_review      — node-http (no curl) review pass that reverts overshoot files
  T8 style_mirror_dir        — loop-start directive: preserve file style exactly
  T9 reread_task_dir         — loop-start directive: re-read criteria before each edit
  T10 hallucinated_suppress  — agent_end hook: delete new files unrelated to task identifiers

Future candidates (not yet validated, do NOT auto-apply):
  asymmetric_canon, plan_exec, hunk_filter, quote_mirror, comment_strip, best_of_n
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

NINJA_ROOT = Path(__file__).resolve().parent
NINJA_AGENT_SRC = NINJA_ROOT / "agent" / "packages" / "agent" / "src"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class TweakResult:
    name: str
    applied: bool
    skipped_reason: str | None = None
    notes: str = ""


def _read(p: Path) -> str:
    return p.read_text()


def _write(p: Path, s: str) -> None:
    p.write_text(s)


def _replace_once(p: Path, old: str, new: str) -> bool:
    c = _read(p)
    if old not in c:
        return False
    if new in c:
        return False  # already replaced
    _write(p, c.replace(old, new, 1))
    return True


def _ensure_after_marker(p: Path, marker: str, snippet: str, dedupe_token: str) -> bool:
    """Insert `snippet` once after the line containing `marker` if `dedupe_token` not present."""
    c = _read(p)
    if dedupe_token in c:
        return False
    if marker not in c:
        return False
    idx = c.find(marker)
    eol = c.find("\n", idx)
    insert_at = eol + 1 if eol != -1 else len(c)
    _write(p, c[:insert_at] + snippet + c[insert_at:])
    return True


# --------------------------------------------------------------------------- #
# T1: PI_TASK_STYLE default off                                               #
# --------------------------------------------------------------------------- #

def t1_task_style_off(repo_root: Path) -> TweakResult:
    """Flip Grit "between-lines" default to "off". Validated: t11 score
    0.032 → 0.139 (~4x lift) when applied."""
    f = repo_root / "agent" / "packages" / "coding-agent" / "src" / "core" / "task-style.ts"
    if not f.is_file():
        return TweakResult("task_style_off", False, "no task-style.ts found")
    content = _read(f)
    if "ninja: disable grit-style" in content.lower() or "NINJA: disable Grit" in content:
        return TweakResult("task_style_off", False, "already applied")
    original_default = (
        'if (!rawMode || rawMode === "between-lines" || rawMode === "1" || rawMode === "true" || rawMode === "yes") {\n'
        '\t\treturn "between-lines";\n'
        '\t}'
    )
    new_default = (
        '// NINJA: disable Grit-style blank-line insertion by default. Reference\n'
        '\t// diffs almost never include blank-line additions; the systematic\n'
        '\t// "blank line between every line" insertion inflates the changed-line\n'
        '\t// denominator and crushes scores. Opt-in only if env explicitly says yes.\n'
        '\tif (rawMode === "between-lines" || rawMode === "1" || rawMode === "true" || rawMode === "yes") {\n'
        '\t\treturn "between-lines";\n'
        '\t}'
    )
    if original_default not in content:
        return TweakResult("task_style_off", False, "task-style.ts shape unrecognised; manual review")
    _write(f, content.replace(original_default, new_default, 1))
    return TweakResult("task_style_off", True, notes="default flipped to off")


# --------------------------------------------------------------------------- #
# T2: File-targeting pre-loop hook + module                                   #
# --------------------------------------------------------------------------- #

def t2_file_targeting(repo_root: Path) -> TweakResult:
    """Copy ninja-target-files.ts and inject pre-loop hook into agent-loop.ts.
    Validated: heuristic identified correct files in 5/6 task offline test."""
    src_module = NINJA_AGENT_SRC / "ninja-target-files.ts"
    if not src_module.is_file():
        return TweakResult("file_targeting", False, "source ninja-target-files.ts missing in toolbox")
    dst_dir = repo_root / "agent" / "packages" / "agent" / "src"
    if not dst_dir.is_dir():
        return TweakResult("file_targeting", False, "no agent/packages/agent/src in target")
    dst_module = dst_dir / "ninja-target-files.ts"
    if not dst_module.exists():
        shutil.copyfile(src_module, dst_module)

    agent_loop = dst_dir / "agent-loop.ts"
    if not agent_loop.is_file():
        return TweakResult("file_targeting", False, "no agent-loop.ts")
    if "ninja-target-files" in _read(agent_loop):
        return TweakResult("file_targeting", False, "hook already present")

    hook = '''
\t// NINJA pre-loop file targeting: scan task text for symbol/path hints,
\t// resolve to high-confidence file paths, inject "primary targets" message
\t// before the first agent turn. Strictly additive — silent when no hits.
\tif (process.env.NINJA_TARGETING !== "0") {
\t\ttry {
\t\t\tlet taskText = "";
\t\t\tfor (const msg of [...context.messages, ...prompts]) {
\t\t\t\tif (!("content" in msg) || !Array.isArray((msg as any).content)) continue;
\t\t\t\tif ((msg as any).role !== "user") continue;
\t\t\t\tfor (const block of (msg as any).content) {
\t\t\t\t\tif (block?.type === "text" && typeof block.text === "string") taskText += block.text + "\\n";
\t\t\t\t}
\t\t\t}
\t\t\tconst { identifyTargetFiles } = await import("./ninja-target-files.js");
\t\t\tconst targeting = identifyTargetFiles(taskText, process.cwd(), 6);
\t\t\tif (targeting.primary.length > 0) {
\t\t\t\tconst list = targeting.primary.map((p) => `\\`${p}\\``).join(", ");
\t\t\t\tcurrentContext.messages.push({
\t\t\t\t\trole: "user",
\t\t\t\t\tcontent: [{ type: "text", text: `Pre-loop file analysis identified these as the most likely target files based on backtick paths, named symbols, and import-graph signals: ${list}. Read each of these BEFORE editing anything else; treat any other file as a candidate only if a hard acceptance criterion explicitly requires it.` }],
\t\t\t\t\ttimestamp: Date.now(),
\t\t\t\t});
\t\t\t}
\t\t} catch { /* targeting is best-effort */ }
\t}

'''
    # Insert just before the first `await runLoop(` call inside runAgentLoop.
    content = _read(agent_loop)
    needle = "await runLoop("
    pos = content.find(needle)
    if pos == -1:
        return TweakResult("file_targeting", False, "couldn't locate runLoop call")
    line_start = content.rfind("\n", 0, pos) + 1
    new_content = content[:line_start] + hook + content[line_start:]
    _write(agent_loop, new_content)
    return TweakResult("file_targeting", True, notes="hook injected before runLoop call")


# --------------------------------------------------------------------------- #
# T3: Blank-run collapse                                                      #
# --------------------------------------------------------------------------- #

def t3_blank_run_collapse(repo_root: Path) -> TweakResult:
    """Backstop pass that collapses 3+ consecutive blank lines down to 1
    after agent_end. Defensive against any path that re-inflates blanks."""
    src_module = NINJA_AGENT_SRC / "ninja-blank-collapse.ts"
    if not src_module.is_file():
        return TweakResult("blank_run_collapse", False, "source module missing")
    dst_dir = repo_root / "agent" / "packages" / "agent" / "src"
    if not dst_dir.is_dir():
        return TweakResult("blank_run_collapse", False, "no agent/packages/agent/src")
    dst_module = dst_dir / "ninja-blank-collapse.ts"
    if not dst_module.exists():
        shutil.copyfile(src_module, dst_module)

    agent_loop = dst_dir / "agent-loop.ts"
    if "ninja-blank-collapse" in _read(agent_loop):
        return TweakResult("blank_run_collapse", False, "already applied")

    hook = '''
\t// NINJA blank-line collapse: defensive backstop in case a downstream pass
\t// re-inflates blank lines. Reduces runs of 3+ consecutive blanks to 1.
\tif (hasProducedEdit && process.env.NINJA_BLANK_COLLAPSE !== "0") {
\t\ttry {
\t\t\tconst { collapseBlankRunsInFile } = await import("./ninja-blank-collapse.js");
\t\t\tfor (const editedPath of editedPaths) {
\t\t\t\tconst norm = editedPath.replace(/^\\.\\//, "");
\t\t\t\tif (!norm || norm.includes("..")) continue;
\t\t\t\ttry { collapseBlankRunsInFile(norm, process.cwd()); } catch { /* skip */ }
\t\t\t}
\t\t} catch { /* best-effort */ }
\t}
'''
    # Insert immediately before the trailing `await emit({ type: "agent_end"` of runLoop.
    content = _read(agent_loop)
    # Find the last `await emit({ type: "agent_end"` (the one outside the early-exit branches).
    matches: list[int] = []
    needle = 'await emit({ type: "agent_end"'
    p = 0
    while True:
        i = content.find(needle, p)
        if i == -1:
            break
        matches.append(i)
        p = i + 1
    if not matches:
        return TweakResult("blank_run_collapse", False, "no agent_end emit found")
    # Last match is the post-loop one. Insert hook just before its line.
    last = matches[-1]
    line_start = content.rfind("\n", 0, last) + 1
    new_content = content[:line_start] + hook + content[line_start:]
    _write(agent_loop, new_content)
    return TweakResult("blank_run_collapse", True, notes="hook injected before final agent_end")


# --------------------------------------------------------------------------- #
# T4: Ensure Edge G is present                                                #
# --------------------------------------------------------------------------- #

def t4_edge_g_ensure(repo_root: Path) -> TweakResult:
    """Verify Edge G (post-edit whitespace-noise cleanup) is present in
    agent-loop.ts. If missing (older base), inject it. Validated: present
    in Mine016 and beneficial there."""
    agent_loop = repo_root / "agent" / "packages" / "agent" / "src" / "agent-loop.ts"
    if not agent_loop.is_file():
        return TweakResult("edge_g_ensure", False, "no agent-loop.ts")
    content = _read(agent_loop)
    if "EDGE G:" in content or "EDGE G " in content or "stripTrailingWs" in content:
        return TweakResult("edge_g_ensure", False, "already present")
    if "editedPaths" not in content:
        return TweakResult("edge_g_ensure", False, "no editedPaths set found; base layout incompatible")

    block = '''
\t// EDGE G: POST-PROCESSING CLEANUP
\t// Strip whitespace-noise diffs the agent may have introduced (trailing
\t// whitespace, line-ending normalisation, identical-modulo-whitespace
\t// replacements). Real human commits rarely include such cosmetic diffs;
\t// they pad the changed-line denominator without scoring matches.
\tif (hasProducedEdit) {
\t\ttry {
\t\t\tconst { spawnSync: _cleanSpawn } = await import("node:child_process");
\t\t\tconst _fs = await import("node:fs");
\t\t\tfor (const editedPath of editedPaths) {
\t\t\t\ttry {
\t\t\t\t\tconst norm = editedPath.replace(/^\\.\\//, "");
\t\t\t\t\tif (!norm || norm.includes("..")) continue;
\t\t\t\t\tif (!_fs.existsSync(norm)) continue;
\t\t\t\t\tconst showResult = _cleanSpawn("git", ["show", `HEAD:${norm}`], {
\t\t\t\t\t\tcwd: process.cwd(), timeout: 1500, encoding: "utf-8", maxBuffer: 8 * 1024 * 1024,
\t\t\t\t\t});
\t\t\t\t\tif (showResult.status !== 0 || typeof showResult.stdout !== "string") continue;
\t\t\t\t\tconst original = showResult.stdout;
\t\t\t\t\tlet current: string;
\t\t\t\t\ttry { current = _fs.readFileSync(norm, "utf-8"); } catch { continue; }
\t\t\t\t\tif (original === current) continue;
\t\t\t\t\tconst stripTrailingWs = (s: string) => s.split(/\\r?\\n/).map((l) => l.replace(/[ \\t]+$/, "")).join("\\n").replace(/\\n+$/, "");
\t\t\t\t\tif (stripTrailingWs(original) === stripTrailingWs(current)) {
\t\t\t\t\t\t_fs.writeFileSync(norm, original, "utf-8");
\t\t\t\t\t\tcontinue;
\t\t\t\t\t}
\t\t\t\t\tconst origLines = original.split(/\\r?\\n/);
\t\t\t\t\tconst currLines = current.split(/\\r?\\n/);
\t\t\t\t\tif (origLines.length === currLines.length) {
\t\t\t\t\t\tlet changed = false;
\t\t\t\t\t\tconst cleaned = currLines.map((c, i) => {
\t\t\t\t\t\t\tconst o = origLines[i];
\t\t\t\t\t\t\tif (o === undefined) return c;
\t\t\t\t\t\t\tif (o === c) return c;
\t\t\t\t\t\t\tif (o.replace(/[ \\t]+$/, "") === c.replace(/[ \\t]+$/, "")) {
\t\t\t\t\t\t\t\tchanged = true;
\t\t\t\t\t\t\t\treturn o;
\t\t\t\t\t\t\t}
\t\t\t\t\t\t\treturn c;
\t\t\t\t\t\t});
\t\t\t\t\t\tif (changed) {
\t\t\t\t\t\t\tconst sep = original.includes("\\r\\n") ? "\\r\\n" : "\\n";
\t\t\t\t\t\t\tconst trailing = original.endsWith("\\n") ? "\\n" : "";
\t\t\t\t\t\t\t_fs.writeFileSync(norm, cleaned.join(sep).replace(/\\n+$/, "") + trailing, "utf-8");
\t\t\t\t\t\t}
\t\t\t\t\t}
\t\t\t\t} catch { /* skip this file */ }
\t\t\t}
\t\t} catch { /* best-effort */ }
\t}
'''
    # Insert before the FINAL agent_end emit (post-loop), like blank_run_collapse.
    matches: list[int] = []
    needle = 'await emit({ type: "agent_end"'
    p = 0
    while True:
        i = content.find(needle, p)
        if i == -1:
            break
        matches.append(i)
        p = i + 1
    if not matches:
        return TweakResult("edge_g_ensure", False, "no agent_end emit found")
    last = matches[-1]
    line_start = content.rfind("\n", 0, last) + 1
    new_content = content[:line_start] + block + content[line_start:]
    _write(agent_loop, new_content)
    return TweakResult("edge_g_ensure", True, notes="injected before final agent_end")


# --------------------------------------------------------------------------- #
# Driver                                                                      #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Helpers for loop-start directive tweaks (T5/T6/T8/T9)                       #
# --------------------------------------------------------------------------- #

def _add_loop_start_directive(repo_root: Path, label: str, dedupe_token: str, text: str) -> TweakResult:
    """Insert a single user-message push into agent-loop.ts before the first
    `await runLoop(` call. The message becomes part of the agent's first turn
    context. Idempotent via a unique comment-line marker."""
    f = repo_root / "agent" / "packages" / "agent" / "src" / "agent-loop.ts"
    if not f.is_file():
        return TweakResult(label, False, "no agent-loop.ts")
    content = _read(f)
    if dedupe_token in content:
        return TweakResult(label, False, "already applied")
    needle = "await runLoop("
    pos = content.find(needle)
    if pos == -1:
        return TweakResult(label, False, "no runLoop call found")
    # Use JSON-escape to avoid quoting issues in the embedded string literal.
    escaped = json.dumps(text)
    line_start = content.rfind("\n", 0, pos) + 1
    snippet = (
        f"\t// {dedupe_token}: ninja loop-start directive\n"
        f"\ttry {{\n"
        f"\t\tcurrentContext.messages.push({{\n"
        f"\t\t\trole: \"user\",\n"
        f"\t\t\tcontent: [{{ type: \"text\", text: {escaped} }}],\n"
        f"\t\t\ttimestamp: Date.now(),\n"
        f"\t\t}});\n"
        f"\t}} catch {{ /* best-effort */ }}\n\n"
    )
    _write(f, content[:line_start] + snippet + content[line_start:])
    return TweakResult(label, True, notes="directive injected")


# --------------------------------------------------------------------------- #
# T5: minimal-change directive                                                #
# --------------------------------------------------------------------------- #

def t5_minimal_change_dir(repo_root: Path) -> TweakResult:
    text = (
        "STRICT MINIMAL-CHANGE POLICY: Modify ONLY what an explicit acceptance "
        "criterion requires. Do not refactor, reformat, rename, re-order imports, "
        "rewrap lines, change quote style, add/remove blank lines, normalise "
        "whitespace, or 'clean up' unrelated regions. Reference diffs are surgical; "
        "every extra line you touch hurts your score."
    )
    return _add_loop_start_directive(repo_root, "T5 minimal_change_dir", "T5_MINIMAL_CHANGE", text)


# --------------------------------------------------------------------------- #
# T6: pre-plan turn                                                           #
# --------------------------------------------------------------------------- #

def t6_pre_plan_turn(repo_root: Path) -> TweakResult:
    text = (
        "BEFORE making any tool calls, reply with a single structured plan listing "
        "(a) every file you will edit (use exact repo-relative paths), and (b) for "
        "each file, the 1-3 line minimum change that satisfies a specific acceptance "
        "criterion. Do NOT call read/edit/write/bash in this first response — only "
        "produce the plan. After the plan, execute it precisely."
    )
    return _add_loop_start_directive(repo_root, "T6 pre_plan_turn", "T6_PRE_PLAN", text)


# --------------------------------------------------------------------------- #
# T8: style-mirror directive                                                  #
# --------------------------------------------------------------------------- #

def t8_style_mirror_dir(repo_root: Path) -> TweakResult:
    text = (
        "STYLE MIRRORING: When editing a file, preserve its existing style EXACTLY: "
        "tab-vs-space indentation and width, single-vs-double quote convention, "
        "trailing semicolon convention, blank-line spacing between declarations, "
        "import ordering, and line endings. Match the surrounding code's style — "
        "do not impose your own preferences."
    )
    return _add_loop_start_directive(repo_root, "T8 style_mirror_dir", "T8_STYLE_MIRROR", text)


# --------------------------------------------------------------------------- #
# T9: re-read task directive                                                  #
# --------------------------------------------------------------------------- #

def t9_reread_task_dir(repo_root: Path) -> TweakResult:
    text = (
        "BEFORE every `edit` or `write` tool call, mentally re-read the task "
        "acceptance criteria and verify your planned change directly satisfies a "
        "specific criterion. If a planned change does not satisfy any explicit "
        "criterion, do NOT make it. Skipping a non-essential edit is always "
        "better than making one that drifts off-target."
    )
    return _add_loop_start_directive(repo_root, "T9 reread_task_dir", "T9_REREAD_TASK", text)


# --------------------------------------------------------------------------- #
# T7: gemini self-review (node:http, no curl)                                 #
# --------------------------------------------------------------------------- #

NINJA_SELF_REVIEW_TS = '''/**
 * Self-review pass: at agent_end, ask the proxy LLM to identify files in the
 * current diff that were over-edited (not required by acceptance criteria)
 * and revert them via `git checkout`. Implementation uses node:http directly
 * because the solver container has no curl binary.
 *
 * Gated by NINJA_SELF_REVIEW=1 (default off until validated against a king).
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";

interface ReviewCfg {
\tmaxReverts: number;
\tmaxDiffChars: number;
\tmodel: string;
}

const CFG: ReviewCfg = {
\tmaxReverts: 3,
\tmaxDiffChars: 30000,
\tmodel: process.env.PI_MODEL || process.env.TAU_MODEL || "docker-proxy-model",
};

function getDiff(cwd: string, maxChars: number): string {
\tconst out = spawnSync("git", ["diff"], { cwd, encoding: "utf-8", maxBuffer: 16 * 1024 * 1024, timeout: 30_000 }).stdout || "";
\treturn out.length <= maxChars ? out : out.slice(0, maxChars) + "\\n... [truncated]";
}

function getChangedFiles(cwd: string): string[] {
\tconst out = spawnSync("git", ["diff", "--name-only"], { cwd, encoding: "utf-8", timeout: 15_000 }).stdout || "";
\treturn out.split("\\n").map((s) => s.trim()).filter(Boolean);
}

async function postJson(host: string, port: number, socket: string | null, path: string, body: string): Promise<string | null> {
\treturn new Promise((resolve) => {
\t\tconst http = require("node:http");
\t\tconst opts: any = {
\t\t\tmethod: "POST",
\t\t\theaders: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
\t\t\tpath,
\t\t};
\t\tif (socket) {
\t\t\topts.socketPath = socket;
\t\t} else {
\t\t\topts.host = host;
\t\t\topts.port = port;
\t\t}
\t\tconst req = http.request(opts, (res: any) => {
\t\t\tlet data = "";
\t\t\tres.setEncoding("utf-8");
\t\t\tres.on("data", (c: string) => { data += c; });
\t\t\tres.on("end", () => resolve(data));
\t\t});
\t\treq.on("error", () => resolve(null));
\t\treq.setTimeout(30_000, () => { req.destroy(); resolve(null); });
\t\treq.write(body);
\t\treq.end();
\t});
}

async function reviewLLM(taskText: string, diff: string, files: string[], cwd: string): Promise<string | null> {
\tconst proxyPort = process.env.TAU_PROXY_LISTEN_PORT || process.env.PI_PROXY_LISTEN_PORT;
\tconst proxySocket = process.env.TAU_PROXY_SOCKET_PATH || process.env.PI_PROXY_SOCKET_PATH;
\tlet host = "127.0.0.1", port = 0, socket: string | null = null;
\tif (proxyPort) { port = parseInt(proxyPort, 10); }
\telse if (proxySocket) { socket = proxySocket; }
\telse { return null; }
\tconst userMsg = `You just produced this diff for the task below. Identify ANY files where the entire change should be reverted because the changes are NOT strictly required by the acceptance criteria. Be CONSERVATIVE — only flag files that are clearly noise/overshoot.\\n\\nOutput format: ONE line per file to revert, prefixed exactly with "REVERT: ". If nothing to revert, output exactly "NONE".\\n\\n=== TASK ===\\n${taskText.slice(0, 4000)}\\n\\n=== CHANGED FILES ===\\n${files.map((f) => "- " + f).join("\\n")}\\n\\n=== DIFF ===\\n${diff}\\n\\n=== YOUR OUTPUT ===`;
\tconst body = JSON.stringify({
\t\tmodel: CFG.model,
\t\tmessages: [
\t\t\t{ role: "system", content: "You review code diffs for over-edits. Output ONLY 'REVERT: <path>' lines or 'NONE'." },
\t\t\t{ role: "user", content: userMsg },
\t\t],
\t\ttemperature: 0,
\t\tmax_tokens: 200,
\t});
\tconst resp = await postJson(host, port, socket, "/v1/chat/completions", body);
\tif (!resp) return null;
\ttry {
\t\tconst parsed = JSON.parse(resp);
\t\tconst text = parsed?.choices?.[0]?.message?.content;
\t\treturn typeof text === "string" ? text : null;
\t} catch { return null; }
}

function parseReverts(text: string, valid: Set<string>): string[] {
\tif (!text || text.trim().toUpperCase() === "NONE") return [];
\tconst out: string[] = [];
\tfor (const line of text.split("\\n")) {
\t\tconst m = line.match(/^\\s*REVERT:\\s*(\\S.*?)\\s*$/);
\t\tif (!m) continue;
\t\tconst p = m[1].replace(/^[`'"]/, "").replace(/[`'"]$/, "").replace(/^\\.\\//, "").trim();
\t\tif (valid.has(p)) out.push(p);
\t}
\treturn out;
}

export async function runSelfReviewPass(taskText: string, cwd: string): Promise<number> {
\tif (process.env.NINJA_SELF_REVIEW === "0") return 0;
\tconst changed = getChangedFiles(cwd);
\tif (changed.length < 2) return 0;
\tconst valid = new Set(changed.map((f) => f.replace(/^\\.\\//, "")));
\tconst diff = getDiff(cwd, CFG.maxDiffChars);
\tif (!diff || diff.length < 100) return 0;
\tconst review = await reviewLLM(taskText, diff, changed, cwd);
\tif (!review) return 0;
\tlet toRevert = parseReverts(review, valid);
\tif (toRevert.length === 0) return 0;
\tif (toRevert.length > CFG.maxReverts) toRevert = toRevert.slice(0, CFG.maxReverts);
\tif (toRevert.length >= changed.length) return 0;
\tlet reverted = 0;
\tfor (const path of toRevert) {
\t\tif (!existsSync(path)) continue;
\t\ttry {
\t\t\tconst r = spawnSync("git", ["checkout", "--", path], { cwd, timeout: 10_000 });
\t\t\tif (r.status === 0) reverted++;
\t\t} catch { /* */ }
\t}
\treturn reverted;
}
'''


def t7_gemini_self_review(repo_root: Path) -> TweakResult:
    """Drop in node-http review module + add hook before final agent_end."""
    dst_dir = repo_root / "agent" / "packages" / "agent" / "src"
    if not dst_dir.is_dir():
        return TweakResult("T7 gemini_self_review", False, "no agent/packages/agent/src")
    dst_module = dst_dir / "ninja-self-review.ts"
    fresh_module = not dst_module.exists()
    if fresh_module:
        _write(dst_module, NINJA_SELF_REVIEW_TS)

    agent_loop = dst_dir / "agent-loop.ts"
    if not agent_loop.is_file():
        return TweakResult("T7 gemini_self_review", False, "no agent-loop.ts")
    content = _read(agent_loop)
    if "T7_SELF_REVIEW_HOOK" in content:
        return TweakResult("T7 gemini_self_review", False, "hook already applied")

    hook = '''
\t// T7_SELF_REVIEW_HOOK: ninja gemini self-review (node:http, gated NINJA_SELF_REVIEW=1)
\tif (process.env.NINJA_SELF_REVIEW === "1") {
\t\ttry {
\t\t\tconst { runSelfReviewPass } = await import("./ninja-self-review.js");
\t\t\tlet taskText = currentContext.systemPrompt || "";
\t\t\tfor (const msg of currentContext.messages) {
\t\t\t\tif (!("content" in msg) || !Array.isArray(msg.content)) continue;
\t\t\t\tfor (const block of msg.content as any[]) {
\t\t\t\t\tif (block?.type === "text" && typeof block.text === "string") taskText += "\\n" + block.text;
\t\t\t\t}
\t\t\t}
\t\t\tawait runSelfReviewPass(taskText, process.cwd());
\t\t} catch { /* best-effort */ }
\t}
'''
    matches: list[int] = []
    needle = 'await emit({ type: "agent_end"'
    p = 0
    while True:
        i = content.find(needle, p)
        if i == -1:
            break
        matches.append(i)
        p = i + 1
    if not matches:
        return TweakResult("T7 gemini_self_review", False, "no agent_end emit found")
    last = matches[-1]
    line_start = content.rfind("\n", 0, last) + 1
    new_content = content[:line_start] + hook + content[line_start:]
    _write(agent_loop, new_content)
    notes = "module written + hook injected" if fresh_module else "hook injected"
    return TweakResult("T7 gemini_self_review", True, notes=notes)


# --------------------------------------------------------------------------- #
# T10: hallucinated-file suppress (post agent_end)                            #
# --------------------------------------------------------------------------- #

NINJA_HALLUCINATION_TS = '''/**
 * Post-edit suppression of hallucinated new files.
 *
 * After agent_end, lists files the agent CREATED (untracked in git, did not
 * exist at HEAD). For each, extracts task identifiers (CamelCase, snake_case,
 * backtick-quoted names from the task text) and checks whether ANY of them
 * appear in the file path or first 2KB of content. If none match, the file
 * is treated as off-target hallucination and unlinked.
 *
 * Bounded: deletes at most 10 files per run, never touches files that
 * existed at HEAD. References almost never add files unrelated to task
 * identifiers, so false-positive deletions are rare.
 */
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import { spawnSync } from "node:child_process";

const COMMON: Set<string> = new Set([
\t"FormData", "Promise", "Date", "Array", "Object", "String", "Number",
\t"Boolean", "Map", "Set", "Error", "JSON", "Math", "Symbol", "Buffer",
\t"File", "Blob", "Event", "Element", "Window", "Document", "Node",
\t"React", "Component", "Props", "State", "Ref",
]);

function extractIdentifiers(taskText: string): Set<string> {
\tconst t = taskText.slice(0, 8000);
\tconst out = new Set<string>();
\tlet m: RegExpExecArray | null;
\tconst re1 = /\\b([A-Z][a-z]+(?:[A-Z][a-z]+|[A-Z]+|\\d+){1,})\\b/g;
\twhile ((m = re1.exec(t)) !== null) { if (m[1].length >= 5 && !COMMON.has(m[1])) out.add(m[1]); }
\tconst re2 = /\\b([a-z]+(?:[A-Z][a-z]+|[A-Z]+){1,})\\b/g;
\twhile ((m = re2.exec(t)) !== null) { if (m[1].length >= 5 && !COMMON.has(m[1])) out.add(m[1]); }
\tconst re3 = /\\b([a-z]+(?:_[a-z0-9]+){1,})\\b/g;
\twhile ((m = re3.exec(t)) !== null) { if (m[1].length >= 5) out.add(m[1]); }
\tconst re4 = /`([A-Za-z_][\\w]{4,40})`/g;
\twhile ((m = re4.exec(t)) !== null) { out.add(m[1]); }
\treturn out;
}

function getNewFiles(cwd: string): string[] {
\tconst r = spawnSync("git", ["ls-files", "--others", "--exclude-standard"], {
\t\tcwd, encoding: "utf-8", timeout: 15_000,
\t});
\tif (r.status !== 0 || !r.stdout) return [];
\treturn r.stdout.split("\\n").map((s) => s.trim()).filter(Boolean);
}

export function suppressHallucinatedFiles(taskText: string, cwd: string, maxDeletes = 10): { deleted: string[]; kept: string[] } {
\tconst deleted: string[] = [];
\tconst kept: string[] = [];
\tconst ids = extractIdentifiers(taskText);
\tif (ids.size === 0) return { deleted, kept };
\tconst newFiles = getNewFiles(cwd);
\tfor (const rel of newFiles) {
\t\tif (deleted.length >= maxDeletes) { kept.push(rel); continue; }
\t\tconst abs = `${cwd}/${rel}`;
\t\tif (!existsSync(abs)) continue;
\t\tlet content = "";
\t\ttry { content = readFileSync(abs, "utf-8").slice(0, 2048); } catch { kept.push(rel); continue; }
\t\tlet hit = false;
\t\tconst hay = (rel + "\\n" + content);
\t\tfor (const id of ids) { if (hay.includes(id)) { hit = true; break; } }
\t\tif (hit) { kept.push(rel); continue; }
\t\ttry { unlinkSync(abs); deleted.push(rel); } catch { kept.push(rel); }
\t}
\treturn { deleted, kept };
}
'''


def t10_hallucinated_suppress(repo_root: Path) -> TweakResult:
    dst_dir = repo_root / "agent" / "packages" / "agent" / "src"
    if not dst_dir.is_dir():
        return TweakResult("T10 hallucinated_suppress", False, "no agent/packages/agent/src")
    dst_module = dst_dir / "ninja-hallucination-suppress.ts"
    fresh = not dst_module.exists()
    if fresh:
        _write(dst_module, NINJA_HALLUCINATION_TS)

    agent_loop = dst_dir / "agent-loop.ts"
    content = _read(agent_loop)
    if "T10_HALLUCINATION_HOOK" in content:
        return TweakResult("T10 hallucinated_suppress", False, "hook already applied")

    hook = '''
\t// T10_HALLUCINATION_HOOK: delete agent-created files unrelated to task identifiers
\tif (hasProducedEdit && process.env.NINJA_SUPPRESS_HALLUCINATED !== "0") {
\t\ttry {
\t\t\tconst { suppressHallucinatedFiles } = await import("./ninja-hallucination-suppress.js");
\t\t\tlet taskText = currentContext.systemPrompt || "";
\t\t\tfor (const msg of currentContext.messages) {
\t\t\t\tif (!("content" in msg) || !Array.isArray(msg.content)) continue;
\t\t\t\tfor (const block of msg.content as any[]) {
\t\t\t\t\tif (block?.type === "text" && typeof block.text === "string") taskText += "\\n" + block.text;
\t\t\t\t}
\t\t\t}
\t\t\tsuppressHallucinatedFiles(taskText, process.cwd());
\t\t} catch { /* best-effort */ }
\t}
'''
    matches: list[int] = []
    needle = 'await emit({ type: "agent_end"'
    p = 0
    while True:
        i = content.find(needle, p)
        if i == -1:
            break
        matches.append(i)
        p = i + 1
    if not matches:
        return TweakResult("T10 hallucinated_suppress", False, "no agent_end emit found")
    last = matches[-1]
    line_start = content.rfind("\n", 0, last) + 1
    new_content = content[:line_start] + hook + content[line_start:]
    _write(agent_loop, new_content)
    notes = "module written + hook injected" if fresh else "hook injected"
    return TweakResult("T10 hallucinated_suppress", True, notes=notes)


# --------------------------------------------------------------------------- #
# Driver                                                                      #
# --------------------------------------------------------------------------- #

TWEAKS: list[tuple[str, Callable[[Path], TweakResult]]] = [
    # ONLY validated tweaks. Each has bench evidence on at least 3 tasks.
    ("T1 task_style_off", t1_task_style_off),  # confirmed ~4x score lift on t11
    ("T4 edge_g_ensure", t4_edge_g_ensure),    # already in Mine016, preservation only
    ("T2 file_targeting", t2_file_targeting),  # 779824e beat king 3-0
    ("T3 blank_run_collapse", t3_blank_run_collapse),  # defensive backstop
    # Held back pending individual validation:
    #   T5 minimal_change_dir, T6 pre_plan_turn, T7 gemini_self_review,
    #   T8 style_mirror_dir, T9 reread_task_dir, T10 hallucinated_suppress
    # These remain DEFINED in this file but are NOT applied. Move to active
    # list ONLY after a head-to-head bench against current king on ≥5 tasks
    # with ≥3% average score uplift.
]


def apply_all(repo_root: Path) -> dict:
    summary: list[dict] = []
    for label, fn in TWEAKS:
        try:
            res = fn(repo_root)
        except Exception as e:
            res = TweakResult(label, False, f"exception: {e}")
        summary.append({
            "tweak": label,
            "applied": res.applied,
            "skipped_reason": res.skipped_reason,
            "notes": res.notes,
        })
    return {"target": str(repo_root), "tweaks": summary}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("target", help="path to cloned fork (must contain agent/ subdir)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    target = Path(args.target).resolve()
    if not target.is_dir():
        print(f"target not a directory: {target}", file=sys.stderr)
        return 2
    if not (target / "agent").is_dir():
        print(f"no agent/ subdir in target — not a tau miner repo?", file=sys.stderr)
        return 2
    summary = apply_all(target)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"toolbox applied to {target}")
        for t in summary["tweaks"]:
            tag = "OK " if t["applied"] else "skip"
            extra = t["notes"] or t["skipped_reason"] or ""
            print(f"  {tag}  {t['tweak']:30s} {extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
