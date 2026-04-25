/**
 * Phase 2 — Mechanical execution of the planned edits.
 *
 * Takes the structured plan from Phase 1 and applies each edit
 * deterministically using the same fuzzy-match logic the `edit` tool uses,
 * but without giving gemini another chance to drift. If an anchor doesn't
 * match exactly, we try increasingly permissive matches:
 *
 *   1. Exact substring match on the file content.
 *   2. Trim-end normalized match (strip trailing whitespace per line on
 *      both anchor and content before comparison).
 *   3. Whitespace-collapsed match (collapse runs of whitespace to single
 *      space on both sides).
 *
 * If all three fail, the edit is skipped and a flag is set so the caller
 * can decide whether to fall through to the standard agent loop for that
 * edit's file.
 *
 * Each edit's success/failure is tracked. The result tells the caller:
 *   - landedFiles: files that received at least one successful edit
 *   - skippedEdits: edits whose anchor couldn't be located
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import type { Plan, PlannedEdit } from "./ninja-plan.js";

export interface ExecuteResult {
	landedFiles: Set<string>;
	successCount: number;
	skipped: PlannedEdit[];
}

function normalizeWs(s: string): string {
	return s.split("\n").map((l) => l.replace(/[ \t]+$/, "")).join("\n");
}

function collapseWs(s: string): string {
	return s.replace(/\s+/g, " ").trim();
}

/**
 * Find the index of `anchor` in `content`. Tries exact, trim-equal, and
 * whitespace-collapsed in that order. Returns the index in the ORIGINAL
 * content (or -1).
 */
function findAnchor(content: string, anchor: string): { index: number; matchLength: number } | null {
	// 1. Exact.
	const exact = content.indexOf(anchor);
	if (exact !== -1) return { index: exact, matchLength: anchor.length };

	// 2. Trim-end normalized.
	const cN = normalizeWs(content);
	const aN = normalizeWs(anchor);
	const trimIdx = cN.indexOf(aN);
	if (trimIdx !== -1) {
		// Map back to original content: count chars in original up to the
		// equivalent normalized position.
		// Simpler: search line-by-line.
		const cLines = content.split("\n");
		const aLines = anchor.split("\n").map((l) => l.replace(/[ \t]+$/, ""));
		for (let i = 0; i + aLines.length <= cLines.length; i++) {
			let ok = true;
			for (let j = 0; j < aLines.length; j++) {
				if (cLines[i + j].replace(/[ \t]+$/, "") !== aLines[j]) {
					ok = false;
					break;
				}
			}
			if (ok) {
				let charIdx = 0;
				for (let k = 0; k < i; k++) charIdx += cLines[k].length + 1;
				let matchLen = 0;
				for (let k = i; k < i + aLines.length; k++) {
					matchLen += cLines[k].length;
					if (k < i + aLines.length - 1) matchLen += 1;
				}
				return { index: charIdx, matchLength: matchLen };
			}
		}
	}

	// 3. Whitespace-collapsed: only useful for short anchors; skip if too
	// risky (>200 chars original).
	if (anchor.length <= 200) {
		const cC = collapseWs(content);
		const aC = collapseWs(anchor);
		if (aC.length > 0 && cC.includes(aC)) {
			// Locate via line search rather than mapping back.
			const cLines = content.split("\n");
			for (let i = 0; i < cLines.length; i++) {
				const window = cLines.slice(i, i + Math.min(5, cLines.length - i)).join("\n");
				if (collapseWs(window).includes(aC)) {
					let charIdx = 0;
					for (let k = 0; k < i; k++) charIdx += cLines[k].length + 1;
					return { index: charIdx, matchLength: window.length };
				}
			}
		}
	}

	return null;
}

/**
 * Apply a single edit to a file. Returns true on success.
 */
function applyEdit(edit: PlannedEdit, cwd: string): boolean {
	const path = `${cwd}/${edit.file.replace(/^\.\//, "")}`;
	if (!existsSync(path)) return false;
	let content: string;
	try { content = readFileSync(path, "utf-8"); } catch { return false; }

	const match = findAnchor(content, edit.anchor);
	if (match === null) return false;

	const { index, matchLength } = match;
	const newContent = content.substring(0, index) + edit.replacement + content.substring(index + matchLength);
	if (newContent === content) return false;

	try {
		writeFileSync(path, newContent, "utf-8");
		return true;
	} catch {
		return false;
	}
}

/**
 * Execute every edit in the plan in order. Returns a result object reporting
 * success rate and any skipped edits.
 */
export function executePlan(plan: Plan, cwd: string): ExecuteResult {
	const landed = new Set<string>();
	const skipped: PlannedEdit[] = [];
	let success = 0;
	for (const edit of plan.edits) {
		if (applyEdit(edit, cwd)) {
			landed.add(edit.file.replace(/^\.\//, ""));
			success++;
		} else {
			skipped.push(edit);
		}
	}
	return { landedFiles: landed, successCount: success, skipped };
}
