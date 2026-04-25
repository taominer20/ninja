/**
 * Phase 1 — Task decomposition / planning.
 *
 * Makes a single dedicated LLM call asking gemini to output a structured
 * edit plan as JSON before any tool execution begins. The plan is then
 * executed mechanically in Phase 2 (ninja-execute.ts), removing gemini's
 * tendency to drift into wrong files / overshoot during free-form tool use.
 *
 * Plan schema:
 *   {
 *     "edits": [
 *       {
 *         "file": "<relative path>",
 *         "anchor": "<exact 1-3 line snippet from current file content; the edit will replace this snippet>",
 *         "replacement": "<new content to put in place of anchor; can be multiple lines>",
 *         "rationale": "<which acceptance criterion this satisfies>"
 *       },
 *       ...
 *     ]
 *   }
 *
 * Failure modes handled:
 *   - JSON parse error: retry once with stricter format directive
 *   - Empty edits: fall back to free-form loop
 *   - All edits invalid: fall back to free-form loop
 *
 * Time budget: this phase has ~40s. Each edit's anchor must be findable
 * in the original file (we don't validate at plan time — Phase 2 handles).
 */

import { existsSync, readFileSync, appendFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

function dbg(msg: string): void {
	if (process.env.NINJA_PLAN_DEBUG !== "1") return;
	try { appendFileSync("/tmp/.ninja_plan.log", `${new Date().toISOString()} ${msg}\n`); } catch { /* ignore */ }
}

export interface PlannedEdit {
	file: string;
	anchor: string;
	replacement: string;
	rationale: string;
}

export interface Plan {
	edits: PlannedEdit[];
}

interface PlanLlmResponse {
	content: string;
}

/** Parse the agent's system prompt for the pre-computed file-discovery
 * sections. Strictly prioritizes "FILES EXPLICITLY NAMED" — the
 * task explicitly mentioned these. Falls back to other sections only if
 * the explicit list is empty. */
function extractFilesFromSystemPrompt(systemPrompt: string): { explicit: string[]; matching: string[]; relevant: string[] } {
	const explicit = new Set<string>();
	const matching = new Set<string>();
	const relevant = new Set<string>();

	const grabBullets = (block: string, sink: Set<string>) => {
		for (const line of block.split("\n")) {
			const bt = line.match(/[-*]\s+`([^`]+)`/);
			if (bt) {
				const p = bt[1].replace(/^\.\//, "").trim();
				if (/\.[a-zA-Z0-9]{1,6}$/.test(p)) sink.add(p);
				continue;
			}
			const plain = line.match(/^[-*]\s+([^\s(]+)/);
			if (plain) {
				const p = plain[1].replace(/^\.\//, "").trim();
				if (/\.[a-zA-Z0-9]{1,6}$/.test(p)) sink.add(p);
			}
		}
	};

	const explicitMatch = systemPrompt.match(/FILES EXPLICITLY NAMED IN THE TASK[^\n]*\n((?:[-*]\s+[^\n]*\n)+)/i);
	if (explicitMatch) grabBullets(explicitMatch[1], explicit);

	const matchingMatch = systemPrompt.match(/FILES MATCHING BY NAME[^\n]*\n((?:[-*]\s+[^\n]*\n)+)/i);
	if (matchingMatch) grabBullets(matchingMatch[1], matching);

	const relevantMatch = systemPrompt.match(/LIKELY RELEVANT FILES[^\n]*\n((?:[-*]\s+[^\n]*\n)+)/i);
	if (relevantMatch) grabBullets(relevantMatch[1], relevant);

	return {
		explicit: [...explicit],
		matching: [...matching],
		relevant: [...relevant],
	};
}

/** Gather candidate target files. STRICT priority:
 *   1. FILES EXPLICITLY NAMED IN THE TASK — these are the targets, period.
 *   2. Backtick paths from task text
 *   3. ONLY if 1+2 empty: top 2 from FILES MATCHING BY NAME
 *   4. ONLY if all empty: top 1 from LIKELY RELEVANT
 *
 * Cap at 3 files. Never include grep-discovered files unless explicit
 * lists are completely empty — those have caused hallucination on prior runs. */
function gatherCandidateFiles(taskText: string, systemPrompt: string, cwd: string): string[] {
	dbg(`systemPrompt length=${systemPrompt.length}`);
	const explicitMatch = systemPrompt.match(/FILES EXPLICITLY NAMED IN THE TASK[^\n]*\n((?:[-*]\s+[^\n]*\n)+)/i);
	dbg(`FILES EXPLICITLY found=${explicitMatch !== null}`);
	if (explicitMatch) dbg(`block first 200: ${explicitMatch[1].slice(0, 200)}`);
	// Also check for the simpler section name variants used elsewhere.
	for (const term of ["LIKELY RELEVANT", "FILES MATCHING", "Pre-identified target", "Named files"]) {
		const i = systemPrompt.indexOf(term);
		if (i >= 0) dbg(`found "${term}" at ${i}: ${systemPrompt.slice(i, i + 200).replace(/\n/g, "\\n")}`);
	}
	const out: string[] = [];
	const seen = new Set<string>();
	const add = (p: string) => {
		if (out.length >= 3) return;
		const norm = p.replace(/^\.\//, "").trim();
		if (!norm || seen.has(norm)) return;
		if (!existsSync(`${cwd}/${norm}`)) return;
		seen.add(norm);
		out.push(norm);
	};

	const sections = extractFilesFromSystemPrompt(systemPrompt);

	// Tier 1: explicit task-named files.
	for (const f of sections.explicit) add(f);

	// Tier 2: backtick paths from task text.
	for (const m of taskText.match(/`([^`\n]{2,200})`/g) || []) {
		const inner = m.slice(1, -1).trim();
		if (/\.[a-zA-Z0-9]{1,6}$/.test(inner)) add(inner);
	}

	// Tier 3: FILES MATCHING BY NAME from system prompt.
	if (out.length === 0) {
		for (const f of sections.matching.slice(0, 2)) add(f);
	}

	// Tier 4: LIKELY RELEVANT from system prompt.
	if (out.length === 0) {
		for (const f of sections.relevant.slice(0, 2)) add(f);
	}

	// Tier 5 (last resort): grep on task identifiers extracted ONLY from
	// task text (not system prompt). Require 2+ distinct identifier hits
	// per file. This is the critical step — earlier I was extracting
	// identifiers from the FULL prompt (including the system prompt's
	// prose) which created spurious matches.
	if (out.length === 0) {
		const identifiers = new Set<string>();
		// Pull task identifiers from BACKTICK quotes only — those are most reliably
		// the actual task targets vs prose noise from any wider context.
		for (const m of taskText.match(/`([^`\n]{4,80})`/g) || []) {
			const inner = m.slice(1, -1).trim();
			if (inner.length >= 4 && !inner.includes("/") && !inner.includes(" ")) identifiers.add(inner);
		}
		// Plus task-text CamelCase, but ONLY from the task itself (first 3KB).
		const taskOnly = taskText.slice(0, 3000);
		for (const m of taskOnly.match(/\b[A-Z][a-z]+(?:[A-Z][a-zA-Z0-9]*)+\b/g) || []) {
			if (m.length >= 6) identifiers.add(m);
		}
		// Remove common JS globals that match too broadly.
		const commonGlobals = new Set(["FormData","Date","Array","Object","String","Number","Boolean","Promise","Map","Set","Error","Math","JSON","RegExp","File","FileReader","Blob","Response","Request","Headers"]);
		for (const c of commonGlobals) identifiers.delete(c);
		dbg(`tier5 identifiers (task-only, no globals): ${[...identifiers].join(",")}`);
		const counts = new Map<string, number>();
		for (const id of [...identifiers].slice(0, 10)) {
			try {
				const r = spawnSync("git", ["grep", "-l", "-F", "--", id, "--", "*.ts", "*.tsx", "*.js", "*.jsx", "*.py", "*.go", "*.rs", "*.rb", "*.java"], { cwd, encoding: "utf-8", timeout: 3000, maxBuffer: 2 * 1024 * 1024 });
				if (r.status === 0 && typeof r.stdout === "string") {
					const seenInThisGrep = new Set<string>();
					for (const f of r.stdout.split("\n").map(s => s.trim()).filter(Boolean)) {
						if (seenInThisGrep.has(f)) continue;
						seenInThisGrep.add(f);
						counts.set(f, (counts.get(f) || 0) + 1);
					}
				}
			} catch { /* skip */ }
		}
		dbg(`tier5 file counts: ${[...counts.entries()].map(([f, c]) => `${f}:${c}`).join(", ")}`);
		// If we have ≤4 candidates total, take all of them (small set is high-signal).
		// Otherwise require ≥2 distinct identifier hits to filter noise.
		const all = [...counts.entries()].sort((a, b) => b[1] - a[1]);
		const ranked = all.length <= 4 ? all.map(([f]) => f) : all.filter(([, c]) => c >= 2).map(([f]) => f);
		for (const f of ranked.slice(0, 3)) add(f);
	}

	dbg(`gatherCandidateFiles result: ${out.join(",")}`);
	return out;
}

/** Read file content trimmed to budget for LLM context. */
function readSnapshot(rel: string, cwd: string, maxChars: number): string {
	try {
		const buf = readFileSync(`${cwd}/${rel}`, "utf-8");
		if (buf.length <= maxChars) return buf;
		return buf.slice(0, maxChars) + "\n... [truncated]";
	} catch {
		return "";
	}
}

/** Strip code fences/extra prose and isolate the first JSON object. */
function extractJson(text: string): string | null {
	if (!text) return null;
	// Remove leading/trailing code-fence markers.
	let t = text.trim();
	t = t.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/g, "");
	// Find first `{` and matching `}`.
	const start = t.indexOf("{");
	if (start === -1) return null;
	let depth = 0;
	let inString = false;
	let escape = false;
	for (let i = start; i < t.length; i++) {
		const c = t[i];
		if (escape) { escape = false; continue; }
		if (c === "\\") { escape = true; continue; }
		if (c === '"') { inString = !inString; continue; }
		if (inString) continue;
		if (c === "{") depth++;
		else if (c === "}") {
			depth--;
			if (depth === 0) return t.slice(start, i + 1);
		}
	}
	return null;
}

function parsePlan(text: string): Plan | null {
	const json = extractJson(text);
	if (!json) return null;
	try {
		const obj = JSON.parse(json);
		if (!obj || !Array.isArray(obj.edits)) return null;
		const edits: PlannedEdit[] = [];
		for (const e of obj.edits) {
			if (!e || typeof e.file !== "string" || typeof e.anchor !== "string" || typeof e.replacement !== "string") continue;
			if (e.file.length === 0 || e.anchor.length < 3) continue;
			edits.push({
				file: e.file.replace(/^\.\//, ""),
				anchor: e.anchor,
				replacement: e.replacement,
				rationale: typeof e.rationale === "string" ? e.rationale : "",
			});
		}
		if (edits.length === 0) return null;
		return { edits };
	} catch {
		return null;
	}
}

/**
 * Call gemini through the docker proxy with a structured planning prompt.
 * Uses curl directly to keep the implementation independent of pi-ai's
 * stream API (which is the agent-loop's domain).
 */
function loadProxyConfig(cwd: string): { baseUrl: string; apiKey: string; modelId: string } | null {
	const configDir = process.env.TAU_CODING_AGENT_DIR || process.env.PI_CODING_AGENT_DIR;
	if (!configDir) return null;
	try {
		const configPath = `${configDir}/models.json`;
		if (!existsSync(configPath)) return null;
		const config = JSON.parse(readFileSync(configPath, "utf-8"));
		const provider = Object.values((config as any).providers || {})[0] as any;
		if (!provider || !provider.baseUrl) return null;
		const modelId = provider.models?.[0]?.id;
		return {
			baseUrl: provider.baseUrl,
			apiKey: provider.apiKey || "",
			modelId: modelId || "google/gemini-2.5-flash",
		};
	} catch { return null; }
}

async function callPlanningLLM(systemMsg: string, userMsg: string, cwd: string): Promise<string | null> {
	const proxyCfg = loadProxyConfig(cwd);
	dbg(`proxyCfg=${proxyCfg ? `baseUrl=${proxyCfg.baseUrl} model=${proxyCfg.modelId}` : "null"}`);
	if (!proxyCfg) return null;
	const url = `${proxyCfg.baseUrl.replace(/\/$/, "")}/chat/completions`;
	const body = JSON.stringify({
		model: proxyCfg.modelId,
		messages: [
			{ role: "system", content: systemMsg },
			{ role: "user", content: userMsg },
		],
		temperature: 0,
		max_tokens: 4000,
	});
	dbg(`POST ${url} bodyLen=${body.length}`);
	try {
		// Use Node's native http module — fetch was hitting "TypeError: terminated"
		// inside the proxy network for reasons we couldn't diagnose. http.request
		// is more permissive about connection edge cases.
		const http = await import("node:http");
		const u = new URL(url);
		const opts = {
			hostname: u.hostname,
			port: u.port,
			path: u.pathname + u.search,
			method: "POST",
			headers: {
				"Content-Type": "application/json",
				"Content-Length": Buffer.byteLength(body).toString(),
				...(proxyCfg.apiKey ? { Authorization: `Bearer ${proxyCfg.apiKey}` } : {}),
			},
		};
		const result: string = await new Promise((resolve, reject) => {
			const req = http.request(opts, (resp) => {
				let chunks: Buffer[] = [];
				resp.on("data", (c) => chunks.push(typeof c === "string" ? Buffer.from(c) : c));
				resp.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
				resp.on("error", reject);
			});
			req.on("error", reject);
			req.setTimeout(60_000, () => req.destroy(new Error("planning request timeout")));
			req.write(body);
			req.end();
		});
		dbg(`http response length=${result.length}, first 200: ${result.slice(0, 200)}`);
		const parsed = JSON.parse(result);
		const content = parsed?.choices?.[0]?.message?.content;
		if (typeof content === "string") return content;
		dbg(`parsed but no content; keys=${Object.keys(parsed || {}).join(",")}`);
		return null;
	} catch (e) {
		dbg(`http exception: ${String(e).slice(0, 200)}`);
		return null;
	}
}

/**
 * Build the planning prompt. Includes:
 *   - Full task text
 *   - Top candidate files (paths + first 8KB of each)
 *   - Strict output format directive
 */
function buildPlanningPrompt(taskText: string, systemPrompt: string, cwd: string): { systemMsg: string; userMsg: string; allowedFiles: string[] } | null {
	const files = gatherCandidateFiles(taskText, systemPrompt, cwd);
	if (files.length === 0) return null;

	const snapshots = files.map((f) => `--- ${f} ---\n${readSnapshot(f, cwd, 6000)}`).join("\n\n");
	const allowedList = files.map((f) => `  - ${f}`).join("\n");

	const systemMsg = `You output a JSON edit plan for a small code task. Your plan will be MECHANICALLY APPLIED — anchors must match the file contents exactly.

STRICT RULES (failure to follow = invalid plan):
1. Output ONLY a JSON object. No code fences, no prose, no markdown.
2. Schema: { "edits": [ { "file": "<path>", "anchor": "<exact snippet>", "replacement": "<new>", "rationale": "<why>" } ] }
3. Each "file" MUST be one of the ALLOWED FILES listed below. Do NOT invent file paths.
4. Each "anchor" MUST be 1-5 consecutive lines copied byte-for-byte from the shown content of that file. Preserve every space, tab, quote, comma, semicolon. If you can't find a unique anchor, OMIT that edit.
5. "replacement" is the new content that fully replaces the anchor.
6. Be SURGICAL — total changes under 150 lines. Plan only what the task requires.
7. If the task is unclear or you cannot reliably plan an edit, return { "edits": [] } and the free-form pass will handle it.`;

	const userMsg = `=== TASK ===
${taskText.slice(0, 5000)}

=== ALLOWED FILES (ONLY these paths are valid for "file" field) ===
${allowedList}

=== CURRENT CONTENT OF ALLOWED FILES ===
${snapshots}

Output JSON now.`;

	return { systemMsg, userMsg, allowedFiles: files };
}

/**
 * Generate a structured edit plan via one LLM call. Returns null if planning
 * fails (parse error, empty edits, no LLM access). Caller should fall back
 * to free-form agent loop on null.
 */
/** Filter plan to only include edits on allowed files. Drops anything else. */
function validatePlan(plan: Plan, allowedFiles: string[]): Plan {
	const allowed = new Set(allowedFiles.map((f) => f.replace(/^\.\//, "")));
	const filtered: PlannedEdit[] = [];
	for (const e of plan.edits) {
		const f = e.file.replace(/^\.\//, "");
		if (allowed.has(f)) filtered.push({ ...e, file: f });
		else dbg(`dropping edit for disallowed file: ${e.file}`);
	}
	return { edits: filtered };
}

export async function generatePlan(taskText: string, systemPrompt: string, cwd: string): Promise<Plan | null> {
	dbg(`enter NINJA_PLAN_EXEC=${process.env.NINJA_PLAN_EXEC}`);
	if (process.env.NINJA_PLAN_EXEC !== "1") return null;
	const prompt = buildPlanningPrompt(taskText, systemPrompt, cwd);
	if (!prompt) {
		dbg("buildPlanningPrompt returned null (no candidate files)");
		return null;
	}
	dbg(`prompt built; userMsg length=${prompt.userMsg.length} allowedFiles=[${prompt.allowedFiles.join(", ")}]`);
	const response = await callPlanningLLM(prompt.systemMsg, prompt.userMsg, cwd);
	if (!response) { dbg("callPlanningLLM returned null"); return null; }
	dbg(`response received; length=${response.length}, first 300: ${response.slice(0, 300)}`);
	let plan = parsePlan(response);
	if (plan === null) {
		dbg("parsePlan returned null on first attempt; retrying");
		const retry = await callPlanningLLM(
			prompt.systemMsg + "\n\nCRITICAL: Output ONLY the JSON object. No surrounding text.",
			prompt.userMsg,
			cwd,
		);
		if (!retry) { dbg("retry returned null"); return null; }
		plan = parsePlan(retry);
		if (plan === null) { dbg("retry parse failed"); return null; }
	}
	plan = validatePlan(plan, prompt.allowedFiles);
	dbg(`plan after validation: ${plan.edits.length} edits`);
	return plan.edits.length > 0 ? plan : null;
}
