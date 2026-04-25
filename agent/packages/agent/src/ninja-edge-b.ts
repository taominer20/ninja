/**
 * Edge B — whole-file cosmetic-only revert.
 *
 * If a file the agent "edited" is identical to original modulo trailing
 * whitespace per line and line-ending style, the agent's changes are entirely
 * cosmetic. Cosmetic-only churn never matches a real reference diff (humans
 * don't produce reference diffs that are pure whitespace), so we restore the
 * original byte-for-byte and remove the file from the diff entirely.
 *
 * This complements Edge A (asymmetric canon, in ninja-pipeline.ts) which
 * handles per-line whitespace; Edge B handles the case where Edge A's
 * residual is still all-noise — the safest move is to revert.
 *
 * Always-on (no env gate). Runs at agent_end after the pipeline.
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

function getOriginalAtHead(rel: string, cwd: string): string | null {
	const r = spawnSync("git", ["show", `HEAD:${rel}`], {
		cwd,
		encoding: "utf-8",
		maxBuffer: 8 * 1024 * 1024,
		timeout: 5000,
	});
	if (r.status !== 0 || typeof r.stdout !== "string") return null;
	return r.stdout;
}

/** Strip per-line trailing whitespace AND normalize CRLF→LF, then compare. */
function trimEqualModWS(a: string, b: string): boolean {
	const norm = (s: string) =>
		s
			.split(/\r?\n/)
			.map((l) => l.replace(/[ \t]+$/, ""))
			.join("\n")
			.replace(/\n+$/, "");
	return norm(a) === norm(b);
}

/**
 * Apply Edge B to all currently-changed files. Returns count of files reverted.
 */
export function applyWholeFileCosmeticRevert(cwd: string): number {
	let reverted = 0;
	const namelist = spawnSync("git", ["diff", "--name-only"], {
		cwd,
		encoding: "utf-8",
		timeout: 15000,
	}).stdout || "";
	const files = namelist.split("\n").map((s) => s.trim()).filter(Boolean);
	for (const rel of files) {
		if (rel.includes("..") || !existsSync(rel)) continue;
		try {
			const original = getOriginalAtHead(rel, cwd);
			if (original === null) continue;
			const current = readFileSync(rel, "utf-8");
			if (original === current) continue;
			if (trimEqualModWS(original, current)) {
				writeFileSync(rel, original, "utf-8");
				reverted++;
			}
		} catch { /* skip */ }
	}
	return reverted;
}
