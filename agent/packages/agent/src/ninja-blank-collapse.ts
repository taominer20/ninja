/**
 * Post-edit blank-line cleanup.
 *
 * Conservative collapse: any run of 3+ consecutive blank lines is reduced
 * to 1 blank line. This is a backstop against accidental blank-spam from
 * the agent's edit pipeline. The bigger lever lives in
 * `coding-agent/src/core/task-style.ts` where ninja disables the
 * inherited Grit-style "between-lines" blank-line insertion entirely.
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";

function isBlank(line: string): boolean {
	return line.trim().length === 0;
}

function collapseBlankRuns(content: string, maxRunLength: number): string {
	const lines = content.split("\n");
	const out: string[] = [];
	let runLen = 0;
	for (const line of lines) {
		if (isBlank(line)) {
			runLen++;
			if (runLen <= maxRunLength) out.push(line);
		} else {
			runLen = 0;
			out.push(line);
		}
	}
	return out.join("\n");
}

export function collapseBlankRunsInFile(
	path: string,
	_cwd: string,
): { changed: boolean; reason: string } {
	if (!existsSync(path)) return { changed: false, reason: "missing" };
	let current: string;
	try { current = readFileSync(path, "utf-8"); } catch { return { changed: false, reason: "read-fail" }; }
	const collapsed = collapseBlankRuns(current, 1);
	if (collapsed === current) return { changed: false, reason: "no-runs" };
	try {
		writeFileSync(path, collapsed, "utf-8");
		return { changed: true, reason: "runs-collapsed" };
	} catch {
		return { changed: false, reason: "write-fail" };
	}
}
