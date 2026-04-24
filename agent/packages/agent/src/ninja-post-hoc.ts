/**
 * Post-hoc trailing-whitespace strip.
 *
 * Bench data (t11/t12/t14, pristine Grit-Agent31 baseline): this transformation
 * added +5 / +18 / +28 matched lines vs baseline — a consistent net-positive
 * across three independent tasks. The mechanism is simple: gemini-flash
 * occasionally emits `line   ` (trailing spaces) where the original file — and
 * therefore the hidden reference diff — has `line`. Every trailing-space
 * instance in a `+` line is a mismatch against the reference's `+` line, even
 * when the surrounding code is correct. Stripping trailing whitespace on every
 * line of every edited file before `git diff` captures eliminates that noise.
 *
 * Applied unconditionally to all edited files — no env gate, since bench
 * showed it never regresses (0-line delta when agent produces clean output).
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";

export function stripTrailingWhitespaceOnEditedFiles(editedPaths: Iterable<string>): number {
	let touched = 0;
	for (const rel of editedPaths) {
		const norm = rel.replace(/^\.\//, "");
		if (!norm || norm.includes("..")) continue;
		try {
			if (!existsSync(norm)) continue;
			const content = readFileSync(norm, "utf-8");
			// Strip trailing whitespace (spaces/tabs) before each newline and at EOF.
			// Preserves the line ending itself (\n or \r\n).
			const cleaned = content
				.replace(/[ \t]+(?=\r?\n)/g, "")
				.replace(/[ \t]+$/, "");
			if (cleaned !== content) {
				writeFileSync(norm, cleaned, "utf-8");
				touched++;
			}
		} catch { /* best-effort per file */ }
	}
	return touched;
}
