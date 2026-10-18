// omp extension for vault-sync. Install: copy to ~/.omp/agent/extensions/vault-sync.ts
//  - first turn of a session: inject the vault router (`vault-init.py context`)
//  - session idle for 10 min, or shutdown: file new knowledge (`vault-capture.py hook omp`)
// Skill location: $VAULT_SYNC_SKILL_DIR, else the first existing standard install dir.
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { execFileSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const IDLE_MS = 10 * 60 * 1000;
const SKILL_DIR =
	process.env.VAULT_SYNC_SKILL_DIR ??
	[".agents", ".claude", ".codex", ".omp/agent"]
		.map((d) => path.join(os.homedir(), d, "skills/vault-sync"))
		.find((d) => existsSync(path.join(d, "scripts/vault-capture.py")));

function capture(sessionFile: string | undefined, sessionId: string): void {
	if (!SKILL_DIR || process.env.VAULT_CAPTURE_RUNNING || !sessionFile) return;
	try {
		spawn("python3", [path.join(SKILL_DIR, "scripts/vault-capture.py"), "hook", "omp", sessionFile, sessionId], {
			detached: true,
			stdio: "ignore",
		}).unref();
	} catch (e) {
		console.error(`[vault-sync] capture spawn failed: ${String(e)}`);
	}
}

export default function (pi: ExtensionAPI) {
	const started = new Set<string>();
	let timer: NodeJS.Timeout | undefined;
	const disarm = (ctx: { clearTimer(handle: NodeJS.Timeout): void }) => {
		if (timer !== undefined) ctx.clearTimer(timer);
		timer = undefined;
	};

	pi.on("before_agent_start", (_event, ctx) => {
		if (!SKILL_DIR || process.env.VAULT_CAPTURE_RUNNING) return;
		if (started.has(ctx.sessionManager.getSessionId())) return;
		let content = "";
		try {
			content = execFileSync("python3", [path.join(SKILL_DIR, "scripts/vault-init.py"), "context"], {
				encoding: "utf8",
				timeout: 5000,
			});
		} catch (e) {
			console.error(`[vault-sync] context failed: ${String(e)}`);
		}
		if (!content.trim()) return;
		return { message: { customType: "vault-sync", content, display: false } };
	});

	pi.on("agent_start", (_event, ctx) => {
		started.add(ctx.sessionManager.getSessionId());
		disarm(ctx);
	});

	pi.on("agent_end", (event, ctx) => {
		if ("isTerminal" in event && event.isTerminal === false) return;
		disarm(ctx);
		const sm = ctx.sessionManager;
		timer = ctx.setTimeout(() => {
			timer = undefined;
			capture(sm.getSessionFile(), sm.getSessionId());
		}, IDLE_MS);
	});

	pi.on("session_shutdown", (_event, ctx) => {
		disarm(ctx);
		capture(ctx.sessionManager.getSessionFile(), ctx.sessionManager.getSessionId());
	});
}
