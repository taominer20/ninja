/**
 * Self-review pass: after the main agent loop completes, send the current
 * diff back to gemini in a single focused review prompt and ask which files
 * are over-edits that should be reverted entirely. Apply the reverts via
 * `git checkout`.
 *
 * Why this can beat the king: gemini-flash systematically overshoots —
 * touches files the task didn't ask for, adds boilerplate, restructures
 * regions that didn't need restructuring. References never overshoot. By
 * asking gemini to critique its own output (a different cognitive task than
 * generating it), we surface the overshoot that the generation pass missed.
 *
 * Cost: one extra LLM call (~5-15s). Fits comfortably in tau's 300s budget
 * since the main loop usually finishes around 60-120s.
 *
 * Output format we ask for: a strict, single line per file to revert:
 *   REVERT: path/to/file.ext
 * Anything else is ignored. If gemini outputs nothing, no reverts.
 */

import { execSync, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";

interface SelfReviewConfig {
	/** Maximum files to revert per task (safety cap). */
	maxReverts: number;
	/** Maximum diff size sent to LLM (in chars). */
	maxDiffChars: number;
	/** OpenRouter-compatible model id (or proxy alias). */
	model: string;
	/** Provider id used by the docker proxy. */
	provider: string;
}

const DEFAULT_CONFIG: SelfReviewConfig = {
	maxReverts: 3,
	maxDiffChars: 30000,
	model: process.env.PI_MODEL || process.env.TAU_MODEL || "docker-proxy-model",
	provider: process.env.PI_PROVIDER || process.env.TAU_PROVIDER || "docker-proxy",
};

function getCurrentDiff(cwd: string, maxChars: number): string {
	const out = spawnSync("git", ["diff"], {
		cwd,
		encoding: "utf-8",
		maxBuffer: 16 * 1024 * 1024,
		timeout: 30_000,
	}).stdout || "";
	if (out.length <= maxChars) return out;
	// Truncate per-file: keep first portion of each file's diff up to max.
	return out.slice(0, maxChars) + "\n... [truncated]";
}

function getChangedFiles(cwd: string): string[] {
	const out = spawnSync("git", ["diff", "--name-only"], {
		cwd,
		encoding: "utf-8",
		timeout: 15_000,
	}).stdout || "";
	return out.split("\n").map((s) => s.trim()).filter(Boolean);
}

/**
 * Make a single LLM completion request via the docker proxy. Uses curl
 * directly to keep the dependency surface minimal (no Node http import,
 * works in any container that has curl which all our images do).
 *
 * Returns the response content text or null on failure.
 */
function llmReview(
	taskText: string,
	diff: string,
	changedFiles: string[],
	cfg: SelfReviewConfig,
	cwd: string,
): string | null {
	const fileList = changedFiles.map((f) => `- ${f}`).join("\n");
	const userMsg = `You just produced this diff for the task below. Review it and identify ANY files where the entire change should be reverted because the changes are NOT strictly required by the acceptance criteria. Be CONSERVATIVE — only flag files that are clearly noise/overshoot.\n\nOutput format: ONE line per file to revert, prefixed exactly with "REVERT: ". If nothing to revert, output exactly "NONE".\n\n=== TASK ===\n${taskText.slice(0, 4000)}\n\n=== CHANGED FILES ===\n${fileList}\n\n=== DIFF ===\n${diff}\n\n=== YOUR OUTPUT (REVERT lines or NONE) ===`;
	const body = JSON.stringify({
		model: cfg.model,
		messages: [
			{ role: "system", content: "You review code diffs for over-edits. Output ONLY 'REVERT: <path>' lines or 'NONE'." },
			{ role: "user", content: userMsg },
		],
		temperature: 0,
		max_tokens: 200,
	});
	// The proxy endpoint depends on env vars set by docker_solver.
	const proxyBridge = process.env.TAU_PROXY_BRIDGE || process.env.PI_PROXY_BRIDGE;
	const proxySocket = process.env.TAU_PROXY_SOCKET_PATH || process.env.PI_PROXY_SOCKET_PATH;
	const proxyPort = process.env.TAU_PROXY_LISTEN_PORT || process.env.PI_PROXY_LISTEN_PORT;
	let url = "";
	if (proxyPort) url = `http://127.0.0.1:${proxyPort}/v1/chat/completions`;
	else if (proxySocket) url = `--unix-socket ${proxySocket} http://localhost/v1/chat/completions`;
	else return null; // No proxy access — bail rather than direct OpenRouter
	try {
		const result = execSync(
			`curl -sS -X POST -H "Content-Type: application/json" -d @- ${url}`,
			{ input: body, encoding: "utf-8", timeout: 30_000, cwd, maxBuffer: 4 * 1024 * 1024 },
		);
		const parsed = JSON.parse(result);
		const text = parsed?.choices?.[0]?.message?.content;
		if (typeof text === "string") return text;
		return null;
	} catch {
		return null;
	}
}

function parseRevertList(reviewText: string, validFiles: Set<string>): string[] {
	if (!reviewText || reviewText.trim().toUpperCase() === "NONE") return [];
	const out: string[] = [];
	for (const line of reviewText.split("\n")) {
		const m = line.match(/^\s*REVERT:\s*(\S.*?)\s*$/);
		if (!m) continue;
		const path = m[1].replace(/^[`'"]/, "").replace(/[`'"]$/, "").replace(/^\.\//, "").trim();
		if (validFiles.has(path)) out.push(path);
	}
	return out;
}

/**
 * Run the self-review pass. Returns count of files reverted.
 */
export async function runSelfReviewPass(taskText: string, cwd: string): Promise<number> {
	if (process.env.NINJA_SELF_REVIEW === "0") return 0;
	const cfg = DEFAULT_CONFIG;
	const changed = getChangedFiles(cwd);
	if (changed.length < 2) return 0; // Single-file diffs aren't candidates for "wrong file" overshoot.
	const validFiles = new Set(changed.map((f) => f.replace(/^\.\//, "")));
	const diff = getCurrentDiff(cwd, cfg.maxDiffChars);
	if (!diff || diff.length < 100) return 0;
	const review = llmReview(taskText, diff, changed, cfg, cwd);
	if (!review) return 0;
	let toRevert = parseRevertList(review, validFiles);
	if (toRevert.length === 0) return 0;
	if (toRevert.length > cfg.maxReverts) toRevert = toRevert.slice(0, cfg.maxReverts);
	// Safety: never revert ALL files.
	if (toRevert.length >= changed.length) return 0;
	let reverted = 0;
	for (const path of toRevert) {
		if (!existsSync(path)) continue;
		try {
			const r = spawnSync("git", ["checkout", "--", path], {
				cwd, timeout: 10_000,
			});
			if (r.status === 0) reverted++;
		} catch { /* skip */ }
	}
	return reverted;
}
