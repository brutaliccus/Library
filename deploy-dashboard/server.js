const express = require("express");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");

const app = express();
const PORT = process.env.PORT || 3999;
const PROJECT_ROOT = path.resolve(__dirname, "..");

// In-memory state (resets on server restart)
let deployState = {
  status: "idle", // idle | running | success | failed
  startedAt: null,
  finishedAt: null,
  exitCode: null,
  log: [],
};

// SSE clients
const clients = new Set();

function broadcast(event, data) {
  const msg = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  clients.forEach((res) => res.write(msg));
}

function runDeploy() {
  if (deployState.status === "running") {
    return { ok: false, message: "Deploy already in progress" };
  }

  deployState = {
    status: "running",
    startedAt: new Date().toISOString(),
    finishedAt: null,
    exitCode: null,
    log: [],
  };
  broadcast("state", deployState);

  const isWindows = process.platform === "win32";
  const deployScript = path.join(PROJECT_ROOT, "deploy.ps1");
  const cmd = isWindows ? "powershell" : "bash";

  let scriptToRun = deployScript;
  let tempScript = null;
  if (isWindows && deployScript.includes(" ")) {
    // Copy to path without spaces (os.tmpdir() can have spaces in username)
    const noSpaceDir = path.resolve(PROJECT_ROOT, "..");
    if (!noSpaceDir.includes(" ")) {
      tempScript = path.join(noSpaceDir, "library-site-deploy.ps1");
      fs.copyFileSync(deployScript, tempScript);
      scriptToRun = tempScript;
    }
  }

  const args = isWindows
    ? ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", scriptToRun]
    : [deployScript];

  const proc = spawn(cmd, args, {
    cwd: PROJECT_ROOT,
    shell: false,
    stdio: ["ignore", "pipe", "pipe"],
  });

  function appendLog(line, type = "stdout") {
    deployState.log.push({ line, type, ts: Date.now() });
    broadcast("log", { line, type });
  }

  proc.stdout.setEncoding("utf8");
  proc.stdout.on("data", (chunk) => {
    chunk.split("\n").forEach((line) => line.trim() && appendLog(line, "stdout"));
  });

  proc.stderr.setEncoding("utf8");
  proc.stderr.on("data", (chunk) => {
    chunk.split("\n").forEach((line) => line.trim() && appendLog(line, "stderr"));
  });

  proc.on("close", (code, signal) => {
    if (tempScript) {
      try { fs.unlinkSync(tempScript); } catch (_) {}
    }
    deployState.status = code === 0 ? "success" : "failed";
    deployState.finishedAt = new Date().toISOString();
    deployState.exitCode = code;
    appendLog(`\n--- Deploy ${code === 0 ? "completed" : "failed"} (exit code: ${code}) ---`, "system");
    broadcast("state", deployState);
  });

  proc.on("error", (err) => {
    if (tempScript) {
      try { fs.unlinkSync(tempScript); } catch (_) {}
    }
    deployState.status = "failed";
    deployState.finishedAt = new Date().toISOString();
    appendLog(`Spawn error: ${err.message}`, "stderr");
    broadcast("state", deployState);
  });

  return { ok: true };
}

// API
app.get("/api/state", (req, res) => {
  res.json(deployState);
});

app.post("/api/deploy", (req, res) => {
  const result = runDeploy();
  res.json(result);
});

// SSE stream
app.get("/api/stream", (req, res) => {
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();

  clients.add(res);
  res.write(`event: state\ndata: ${JSON.stringify(deployState)}\n\n`);

  req.on("close", () => clients.delete(res));
});

// Static dashboard
app.use(express.static(path.join(__dirname, "public")));

app.listen(PORT, () => {
  console.log(`Deploy dashboard: http://localhost:${PORT}`);
});
