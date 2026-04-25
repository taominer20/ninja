/**
 * Pre-loop file-targeting heuristic.
 *
 * Most failed solves on this benchmark are file-disambiguation failures:
 * gemini picks the wrong file (e.g. VisaApplicationForm.tsx when the
 * reference touches VisaApplicationPage.tsx) and scores zero. The system
 * prompt's "FILES EXPLICITLY NAMED" section helps when the task quotes
 * exact paths, but most tasks describe symbols, components, and behaviors
 * without quoting any path at all.
 *
 * This module scans the task text for high-signal hints and hardens them
 * into a "primary targets" list to inject upfront. Hints, in order:
 *
 *   1. Quoted/backticked paths in the task text (highest confidence).
 *   2. Bare file-name tokens like `Foo.tsx` or `bar/baz.py`.
 *   3. CamelCase identifiers (function/component names, e.g.
 *      `createVisaApplication`, `VisaApplicationPage`). For each
 *      identifier, locate files in the repo that DEFINE it (export
 *      function, class, const declaration, default export).
 *   4. snake_case / kebab-case identifiers above 4 chars.
 *
 * Output: ranked list of repo-relative file paths, capped at 8.
 *
 * The agent-loop hook injects this list as a single high-priority user
 * message BEFORE the agent's first response, so gemini anchors on the
 * right files from turn 1 instead of wandering through the file tree.
 */

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const FILE_EXT = /\.(?:tsx?|jsx?|py|go|rs|java|kt|swift|rb|php|cs|cpp|cc|h|hpp|c|md|json|ya?ml|toml|css|scss|sql|sh|bash|zsh|env)$/i;
const SOURCE_EXT = /\.(?:tsx?|jsx?|py|go|rs|java|kt|swift|rb|php|cs|cpp|cc|h|hpp|c|sh|bash)$/i;
const SKIP_DIRS = new Set([
	"node_modules", ".git", ".next", "dist", "build", "out", "target",
	".turbo", ".cache", "coverage", "venv", "__pycache__", ".venv",
	"vendor", ".idea", ".vscode", "bower_components",
]);

const COMMON_TOKENS = new Set([
	"FormData", "Promise", "Date", "Array", "Object", "String", "Number",
	"Boolean", "Map", "Set", "Error", "JSON", "Math", "Symbol", "Buffer",
	"File", "Blob", "Event", "Element", "Window", "Document", "Node",
	"React", "Component", "Props", "State", "Ref", "useState", "useEffect",
	"useRef", "useMemo", "useCallback", "useContext", "useReducer",
	"Fragment", "Children", "Provider", "Context",
	"True", "False", "None", "Self", "Args", "Kwargs",
	"Page", "Form", "List", "Item", "Card", "Modal", "Button", "Input",
	"Header", "Footer", "Body", "Title", "Label", "Field",
	"Get", "Post", "Put", "Delete", "Head", "Patch",
	"Route", "Router", "Link", "Path",
	"Test", "Mock", "Spec", "Suite", "Setup", "Before", "After",
]);

interface ExtractedHints {
	quotedPaths: string[];
	bareFiles: string[];
	camelIds: string[];
	snakeIds: string[];
}

function extractHints(taskText: string): ExtractedHints {
	const text = taskText.slice(0, 8000);
	const quotedPaths = new Set<string>();
	const bareFiles = new Set<string>();
	const camelIds = new Set<string>();
	const snakeIds = new Set<string>();

	const quotedRe = /[`'"]([\w./-]+\/[\w./-]+|[\w-]+\.[a-zA-Z]{1,5})[`'"]/g;
	let m: RegExpExecArray | null;
	while ((m = quotedRe.exec(text)) !== null) {
		const candidate = m[1];
		if (candidate.length > 200) continue;
		if (FILE_EXT.test(candidate) || candidate.includes("/")) {
			quotedPaths.add(candidate);
		}
	}

	const bareRe = /\b([A-Za-z_][\w.-]*\.[a-zA-Z]{1,5})\b/g;
	while ((m = bareRe.exec(text)) !== null) {
		const tok = m[1];
		if (FILE_EXT.test(tok) && tok.length < 80 && !tok.includes("..")) {
			if (!quotedPaths.has(tok)) bareFiles.add(tok);
		}
	}

	const upperCamelRe = /\b([A-Z][a-z]+(?:[A-Z][a-z]+|[A-Z]+|\d+){1,})\b/g;
	while ((m = upperCamelRe.exec(text)) !== null) {
		const id = m[1];
		if (id.length < 7 || id.length > 60) continue;
		if (COMMON_TOKENS.has(id)) continue;
		camelIds.add(id);
	}

	const lowerCamelRe = /\b([a-z]+(?:[A-Z][a-z]+|[A-Z]+){1,})\b/g;
	while ((m = lowerCamelRe.exec(text)) !== null) {
		const id = m[1];
		if (id.length < 7 || id.length > 60) continue;
		if (COMMON_TOKENS.has(id)) continue;
		camelIds.add(id);
	}

	const backtickIdRe = /`([A-Za-z_][\w]{4,40})`/g;
	while ((m = backtickIdRe.exec(text)) !== null) {
		const id = m[1];
		if (id.length < 5) continue;
		if (COMMON_TOKENS.has(id) || id.includes(".")) continue;
		camelIds.add(id);
	}

	const snakeRe = /\b([a-z]+(?:_[a-z0-9]+){1,})\b/g;
	while ((m = snakeRe.exec(text)) !== null) {
		const id = m[1];
		if (id.length < 7 || id.length > 60) continue;
		if (COMMON_TOKENS.has(id)) continue;
		snakeIds.add(id);
	}

	return {
		quotedPaths: [...quotedPaths],
		bareFiles: [...bareFiles],
		camelIds: [...camelIds],
		snakeIds: [...snakeIds],
	};
}

function listSourceFiles(root: string, max = 4000): string[] {
	const out: string[] = [];
	const stack: string[] = [root];
	while (stack.length > 0 && out.length < max) {
		const dir = stack.pop()!;
		let entries: string[] = [];
		try { entries = readdirSync(dir); } catch { continue; }
		for (const entry of entries) {
			if (entry.startsWith(".") && entry !== "." && entry !== "..") {
				if (SKIP_DIRS.has(entry)) continue;
			}
			if (SKIP_DIRS.has(entry)) continue;
			const full = join(dir, entry);
			let st;
			try { st = statSync(full); } catch { continue; }
			if (st.isDirectory()) {
				stack.push(full);
			} else if (st.isFile() && SOURCE_EXT.test(entry)) {
				out.push(full);
				if (out.length >= max) break;
			}
		}
	}
	return out;
}

interface Score {
	path: string;
	score: number;
	reason: string;
}

function scoreFiles(repoRoot: string, hints: ExtractedHints): Score[] {
	const scoredMap = new Map<string, Score>();
	const add = (p: string, delta: number, reason: string) => {
		const rel = p.startsWith(repoRoot) ? relative(repoRoot, p) : p;
		const cur = scoredMap.get(rel);
		if (cur) {
			cur.score += delta;
			if (!cur.reason.includes(reason)) cur.reason += `; ${reason}`;
		} else {
			scoredMap.set(rel, { path: rel, score: delta, reason });
		}
	};

	const srcFiles = listSourceFiles(repoRoot, 4000);

	for (const qp of hints.quotedPaths) {
		const candidate = qp.replace(/^\.\//, "");
		const full = join(repoRoot, candidate);
		if (existsSync(full)) {
			add(full, 100, "quoted-path");
		} else {
			for (const f of srcFiles) {
				if (f.endsWith("/" + candidate) || f.endsWith(candidate)) {
					add(f, 80, "quoted-suffix");
				}
			}
		}
	}

	for (const bf of hints.bareFiles) {
		for (const f of srcFiles) {
			if (f.endsWith("/" + bf)) {
				add(f, 60, `bare-file:${bf}`);
			}
		}
	}

	const allIds = [...hints.camelIds, ...hints.snakeIds];
	const fileContents = new Map<string, string>();
	for (const f of srcFiles) {
		try {
			const c = readFileSync(f, "utf-8");
			if (c.length <= 200_000) fileContents.set(f, c);
		} catch { /* skip */ }
	}

	const inUseMap = new Map<string, number>();
	const importPathRe = /import\s+(?:[\w*{},\s]+from\s+)?['"]([^'"]+)['"]/g;
	for (const c of fileContents.values()) {
		let mm: RegExpExecArray | null;
		while ((mm = importPathRe.exec(c)) !== null) {
			const p = mm[1];
			if (!p.startsWith(".") && !p.startsWith("/")) continue;
			const lastSeg = p.split("/").pop() || "";
			inUseMap.set(lastSeg, (inUseMap.get(lastSeg) || 0) + 1);
		}
	}

	if (allIds.length > 0) {
		for (const [f, content] of fileContents) {
			let fileScore = 0;
			const matched: string[] = [];
			for (const id of allIds) {
				const defRe = new RegExp(
					`(?:^|\\n)\\s*(?:export\\s+(?:default\\s+)?)?` +
					`(?:async\\s+)?(?:function|class|const|let|var|interface|type|def|fn)\\s+${id}\\b`,
					"g",
				);
				const importRe = new RegExp(
					`import\\s*\\{[^}]*\\b${id}\\b[^}]*\\}|from\\s+['"][^'"]*${id}[^'"]*['"]`,
					"g",
				);
				const callRe = new RegExp(`\\b${id}\\s*\\(`, "g");
				if (defRe.test(content)) { fileScore += 50; matched.push(`def:${id}`); }
				else if (importRe.test(content)) { fileScore += 35; matched.push(`imp:${id}`); }
				else if (callRe.test(content)) { fileScore += 25; matched.push(`call:${id}`); }
				else if (content.includes(id)) fileScore += 10;
			}
			if (fileScore > 0) {
				const baseName = f.split("/").pop()?.replace(/\.\w+$/, "") || "";
				const inUse = inUseMap.get(baseName) || 0;
				if (inUse > 0) {
					fileScore += Math.min(inUse * 5, 20);
					matched.push(`inUse:${inUse}`);
				}
				const reason = matched.length > 0 ? matched.slice(0, 4).join(",") : "mentions";
				add(f, fileScore, reason);
			}
		}
	}

	return [...scoredMap.values()]
		.filter((s) => s.score >= 30)
		.sort((a, b) => b.score - a.score);
}

export interface TargetingResult {
	primary: string[];
	debug: { hints: ExtractedHints; scored: { path: string; score: number; reason: string }[] };
}

/**
 * Identify primary target files for the task. Returns up to `maxFiles`
 * repo-relative paths, ranked by combined hint signal. Empty list means
 * no high-confidence targets — caller should NOT inject a misleading
 * "primary files" message in that case.
 */
export function identifyTargetFiles(taskText: string, repoRoot: string, maxFiles = 6): TargetingResult {
	if (!taskText || taskText.length < 30) {
		return { primary: [], debug: { hints: { quotedPaths: [], bareFiles: [], camelIds: [], snakeIds: [] }, scored: [] } };
	}
	const hints = extractHints(taskText);
	const scored = scoreFiles(repoRoot, hints);
	const primary = scored.slice(0, maxFiles).map((s) => s.path);
	return { primary, debug: { hints, scored: scored.slice(0, 12) } };
}
