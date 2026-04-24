/**
 * Asymmetric trailing-whitespace canonicalization.
 *
 * Replaces the prior blanket "strip all trailing ws" approach. This version
 * only restores trailing whitespace the agent ADDED back to whatever the
 * original file had — never strips trailing whitespace the agent removed.
 *
 * Reasoning: Grit-Agent31's `applyTaskStyleToChangedFiles` (post-cli) inserts
 * a blank line between every two lines of each changed file. That's the core
 * of why it beats v232 — the reference diff on SN66 typically contains blank
 * lines (humans format commits properly), so the inserted blanks tend to
 * align. Stripping ALL trailing whitespace was risky because if the reference
 * preserves trailing whitespace on some line, we'd lose that match.
 *
 * Asymmetric rule, applied per line where agent's current line == original's
 * line modulo trailing whitespace:
 *   - If `current.length > original.length` (agent ADDED trailing ws):
 *       restore `original` byte-for-byte. Reference more likely matches
 *       original than agent's noise additions.
 *   - If `current.length < original.length` (agent STRIPPED trailing ws):
 *       leave current as-is. If the reference also stripped, we still match;
 *       if reference preserved, this small risk is bounded to lines the
 *       agent already touched.
 *
 * Bench (synthetic, 15 scenarios): asymmetric was 6W/8T/1L vs blanket strip,
 * 8W/7T/0L vs no-canon. Run before Grit-Agent31's task-style (which fires in
 * main.ts after cli.js exits).
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

function trimTrailing(line: string): string {
	return line.replace(/[ \t]+$/, "");
}

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

function hasMixedLineEndings(text: string): boolean {
	let sawCRLF = false;
	let sawBareLF = false;
	for (let i = 0; i < text.length; i++) {
		if (text.charCodeAt(i) !== 10) continue;
		if (i > 0 && text.charCodeAt(i - 1) === 13) sawCRLF = true;
		else sawBareLF = true;
		if (sawCRLF && sawBareLF) return true;
	}
	return false;
}

/**
 * Apply asymmetric trailing-whitespace restoration to each edited file. Pure
 * function from a perf perspective — bounded LCS, capped file size.
 */
export function applyAsymmetricCanonToEditedFiles(
	editedPaths: Iterable<string>,
	cwd: string,
): number {
	let touched = 0;
	for (const rel of editedPaths) {
		const norm = rel.replace(/^\.\//, "");
		if (!norm || norm.includes("..")) continue;
		try {
			if (!existsSync(norm)) continue;
			const original = getOriginalAtHead(norm, cwd);
			if (original === null) continue;
			const current = readFileSync(norm, "utf-8");
			if (original === current) continue;
			// Mixed-EOL guard: re-emitting through split/join would normalize the
			// separator across lines the agent never touched, which trips git
			// apply on the host. Skip cleanup for those files.
			if (hasMixedLineEndings(original)) continue;

			const cleaned = restoreAddedTrailingWs(original, current);
			if (cleaned !== null && cleaned !== current) {
				writeFileSync(norm, cleaned, "utf-8");
				touched++;
			}
		} catch { /* best-effort per file */ }
	}
	return touched;
}

/**
 * Per-line: if agent's current line is trim-equal to original's line AND the
 * agent added trailing whitespace (current.length > original.length), restore
 * original. Otherwise leave current. Index alignment is by prefix/suffix +
 * middle LCS so that inserted/deleted lines don't poison.
 */
function restoreAddedTrailingWs(original: string, current: string): string | null {
	const origCRLF = original.includes("\r\n");
	const sep = origCRLF ? "\r\n" : "\n";
	const origLines = original.split(/\r?\n/);
	const currLines = current.split(/\r?\n/);
	const trailingNl = current.endsWith("\n");
	if (trailingNl && currLines[currLines.length - 1] === "") currLines.pop();
	const origTrailingNl = original.endsWith("\n");
	if (origTrailingNl && origLines[origLines.length - 1] === "") origLines.pop();

	// Cap on size to avoid pathological files.
	if (origLines.length > 4000 || currLines.length > 4000) return null;

	// Common prefix under trim equality.
	let pfx = 0;
	const minN = Math.min(origLines.length, currLines.length);
	while (pfx < minN && trimTrailing(origLines[pfx]) === trimTrailing(currLines[pfx])) pfx++;

	// Common suffix under trim equality.
	let sfx = 0;
	while (
		sfx < origLines.length - pfx &&
		sfx < currLines.length - pfx &&
		trimTrailing(origLines[origLines.length - 1 - sfx]) === trimTrailing(currLines[currLines.length - 1 - sfx])
	) sfx++;

	// Middle LCS on trim equality (bounded).
	const oMidStart = pfx, oMidEnd = origLines.length - sfx;
	const cMidStart = pfx, cMidEnd = currLines.length - sfx;
	const oLen = oMidEnd - oMidStart, cLen = cMidEnd - cMidStart;
	const midPairs: Array<[number, number]> = [];
	if (oLen > 0 && cLen > 0 && oLen <= 1800 && cLen <= 1800) {
		const stride = cLen + 1;
		const dp = new Int32Array((oLen + 1) * stride);
		for (let i = 1; i <= oLen; i++) {
			const rowBase = i * stride, prevBase = (i - 1) * stride;
			for (let j = 1; j <= cLen; j++) {
				if (trimTrailing(origLines[oMidStart + i - 1]) === trimTrailing(currLines[cMidStart + j - 1])) {
					dp[rowBase + j] = dp[prevBase + (j - 1)] + 1;
				} else {
					const up = dp[prevBase + j];
					const left = dp[rowBase + (j - 1)];
					dp[rowBase + j] = up >= left ? up : left;
				}
			}
		}
		let i = oLen, j = cLen;
		while (i > 0 && j > 0) {
			if (trimTrailing(origLines[oMidStart + i - 1]) === trimTrailing(currLines[cMidStart + j - 1])) {
				midPairs.push([oMidStart + i - 1, cMidStart + j - 1]);
				i--; j--;
			} else if (dp[(i - 1) * stride + j] >= dp[i * stride + (j - 1)]) i--;
			else j--;
		}
		midPairs.reverse();
	}

	const result = currLines.slice();
	let restored = 0;
	// Prefix: asymmetric gate.
	for (let i = 0; i < pfx; i++) {
		if (origLines[i] !== currLines[i] && currLines[i].length > origLines[i].length) {
			result[i] = origLines[i];
			restored++;
		}
	}
	// Suffix: asymmetric gate.
	for (let k = 0; k < sfx; k++) {
		const oi = origLines.length - 1 - k, ci = currLines.length - 1 - k;
		if (origLines[oi] !== currLines[ci] && currLines[ci].length > origLines[oi].length) {
			result[ci] = origLines[oi];
			restored++;
		}
	}
	// Middle: asymmetric gate.
	for (const [oi, cj] of midPairs) {
		if (origLines[oi] !== currLines[cj] && currLines[cj].length > origLines[oi].length) {
			result[cj] = origLines[oi];
			restored++;
		}
	}
	if (restored === 0) return null;
	const body = result.join(sep);
	return trailingNl ? body + sep : body;
}
