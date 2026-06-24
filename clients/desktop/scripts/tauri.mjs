#!/usr/bin/env node
import { existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

const args = process.argv.slice(2);
const cwd = process.cwd();
const sep = path.sep;

function isThrowawayWorktree(dir) {
  const normalized = path.resolve(dir);
  const parts = normalized.split(sep);
  return (
    parts.includes(".worktrees") ||
    parts.includes(".audit-worktrees") ||
    normalized.includes(`${sep}.claude${sep}worktrees${sep}`) ||
    normalized.includes(`${sep}.alfred${sep}worktrees${sep}`)
  );
}

const env = { ...process.env };
if (!env.CARGO_TARGET_DIR && isThrowawayWorktree(cwd)) {
  const targetDir = path.join(homedir(), ".alfred", "cargo-target", "desktop");
  mkdirSync(targetDir, { recursive: true });
  env.CARGO_TARGET_DIR = targetDir;
  console.error(`[alfred-tauri] CARGO_TARGET_DIR=${targetDir}`);
}

function tauriBin() {
  const name = process.platform === "win32" ? "tauri.cmd" : "tauri";
  const local = path.join(cwd, "node_modules", ".bin", name);
  return existsSync(local) ? local : name;
}

const bin = tauriBin();
const child = spawn(bin, args, {
  cwd,
  env,
  stdio: "inherit",
});

child.on("error", (error) => {
  console.error(`[alfred-tauri] failed to start ${bin}: ${error.message}`);
  process.exit(127);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
