#!/usr/bin/env node
// One-command startup for the app. Works the same on Windows, macOS and Linux.
//
//   node run.mjs          build the frontend if it changed, then serve everything
//                           from the backend at http://localhost:8000
//   node run.mjs --dev    backend + Vite dev server with hot reload,
//                           open http://localhost:5173
//
// Wrappers: run.cmd (Windows: `.\run` or `.\run --dev`) and run.sh.
// This only runs the same commands as the README; it changes nothing else.

import { spawn, spawnSync } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(ROOT, "frontend");
const BACKEND = join(ROOT, "backend");
const WIN = process.platform === "win32";

const args = process.argv.slice(2);
const DEV = args.includes("--dev");
if (args.some((a) => a !== "--dev")) {
  console.log("Usage: node run.mjs [--dev]");
  process.exit(args.includes("--help") || args.includes("-h") ? 0 : 1);
}

function log(msg) {
  console.log(`[run] ${msg}`);
}

function fail(msg) {
  console.error(`[run] ${msg}`);
  process.exit(1);
}

// npm is npm.cmd on Windows, which needs a shell to run.
function run(cmd, cmdArgs, cwd) {
  const r = spawnSync(cmd, cmdArgs, { cwd, stdio: "inherit", shell: WIN });
  if (r.status !== 0) fail(`"${cmd} ${cmdArgs.join(" ")}" failed in ${cwd}`);
}

function have(cmd) {
  const r = spawnSync(cmd, ["--version"], { stdio: "ignore", shell: WIN });
  return r.status === 0;
}

function newestMtime(path) {
  if (!existsSync(path)) return 0;
  const s = statSync(path);
  if (!s.isDirectory()) return s.mtimeMs;
  let newest = 0;
  for (const name of readdirSync(path)) newest = Math.max(newest, newestMtime(join(path, name)));
  return newest;
}

// --- checks ---------------------------------------------------------------

if (!have("uv")) fail("uv is not installed or not on PATH. See https://docs.astral.sh/uv/");
if (!have("npm")) fail("npm is not installed or not on PATH. Install Node 20+.");

if (!existsSync(join(BACKEND, ".env"))) {
  log("backend/.env not found. The map works, but the agent needs ANTHROPIC_API_KEY.");
  log("Create it from backend/.env.example (see README).");
}

if (!existsSync(join(FRONTEND, "node_modules"))) {
  log("Installing frontend packages (first run only)...");
  run("npm", ["install"], FRONTEND);
}

// --- build (normal mode only) ---------------------------------------------

if (!DEV) {
  const built = newestMtime(join(FRONTEND, "dist", "index.html"));
  const sources = Math.max(
    newestMtime(join(FRONTEND, "src")),
    newestMtime(join(FRONTEND, "index.html")),
    newestMtime(join(FRONTEND, "package.json")),
    newestMtime(join(FRONTEND, "vite.config.ts")),
  );
  if (built === 0 || sources > built) {
    log("Building the frontend...");
    run("npm", ["run", "build"], FRONTEND);
  } else {
    log("Frontend build is up to date.");
  }
}

// --- run ------------------------------------------------------------------

const children = [];
let stopping = false;

function startChild(name, cmd, cmdArgs, cwd) {
  // On macOS/Linux give each child its own process group so stopping it also
  // stops what it started (npm -> vite, uv -> uvicorn).
  const child = spawn(cmd, cmdArgs, { cwd, stdio: "inherit", shell: WIN, detached: !WIN });
  child.on("exit", (code) => {
    if (stopping) return;
    log(`${name} exited${code === null ? "" : ` with code ${code}`}. Stopping.`);
    stopAll(code ?? 1);
  });
  children.push(child);
}

function stopAll(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const c of children) {
    if (c.exitCode !== null) continue;
    // On Windows the child is a shell; kill its whole tree so nothing keeps the port.
    if (WIN) spawnSync("taskkill", ["/pid", String(c.pid), "/T", "/F"], { stdio: "ignore" });
    else {
      try {
        process.kill(-c.pid, "SIGTERM");
      } catch {
        c.kill("SIGTERM");
      }
    }
  }
  setTimeout(() => process.exit(code), 300);
}

process.on("SIGINT", () => stopAll(0));
process.on("SIGTERM", () => stopAll(0));

// Wait for the backend before starting Vite, so the first page load does not
// hit a proxy error while uv is still syncing or uvicorn is still starting.
async function waitForBackend(timeoutMs = 180_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until && !stopping) {
    try {
      await fetch("http://127.0.0.1:8000/api/course");
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  return false;
}

startChild("backend", "uv", ["run", "uvicorn", "app.main:app", "--port", "8000"], BACKEND);
if (DEV) {
  log("Waiting for the backend...");
  if (!(await waitForBackend())) {
    if (!stopping) log("Backend did not answer on port 8000. Starting Vite anyway.");
  }
  if (!stopping) {
    startChild("frontend", "npm", ["run", "dev"], FRONTEND);
    log("Dev mode: open http://localhost:5173 (hot reload). Ctrl+C stops both.");
  }
} else {
  log("Open http://localhost:8000. Ctrl+C to stop.");
}
