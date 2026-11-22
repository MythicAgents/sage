// Private Unix-socket bridge from Sage to the Codex ACP adapter's stdio.
//
// Each accepted connection gets its own adapter process (own process group), working directory and
// CODEX_HOME under /state, a copy of the read-only Codex configuration template (or, without one, a configuration
// holding only the default model from CODEX_MODEL), and the mounted API-key secret in the child's environment when
// one is provisioned (a mounted secret file, or CODEX_API_KEY). ACP's newline-delimited JSON-RPC is relayed byte for byte;
// the bridge reads only each message's method/id to log what crossed it, never its parameters. A line over 1 MiB
// in either direction ends the connection. End of stream, error or shutdown closes the adapter's stdin, then
// sends TERM and KILL to its process group within five seconds, and removes the connection's state directory.
// Run the container with an init process (`init: true` / `--init`) so orphaned grandchildren are reaped.

import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";

const SOCKET = "/transport/acp.sock";
const STATE_ROOT = "/state";
const CONFIG_TEMPLATE = "/config/config.toml";
const SECRET_FILE = "/run/secrets/codex_api_key";
// A model ID only: written into TOML, so anything that could break out of the quoted string is refused.
const MODEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}$/;
const DEFAULT_MODEL = (process.env.CODEX_MODEL || "").trim();
const DEFAULT_MODEL_OK = DEFAULT_MODEL !== "" && MODEL_PATTERN.test(DEFAULT_MODEL);
const ADAPTER = [process.execPath, "/opt/codex-acp/node_modules/@agentclientprotocol/codex-acp/dist/index.js"];
const MAX_CONNECTIONS = 4;
const MAX_LINE_BYTES = 1024 * 1024;
const GRACEFUL_MS = 1500;
const TERM_MS = 1500;
const KILL_MS = 1500;
const BUSY_FIRST_LINE_MS = 5000;
const PASS_ENV = [
  "LANG", "TZ", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
  "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
];

process.umask(0o077);
let counter = 0;
const active = new Map();

function log(event) {
  process.stdout.write(`${JSON.stringify({ time: new Date().toISOString(), ...event })}\n`);
}

// The key comes from a mounted secret file when one is provided, otherwise from CODEX_API_KEY (the compose file's
// `.env`). Only the adapter process receives it.
function readSecret() {
  try {
    const value = fs.readFileSync(SECRET_FILE, "utf8").trim();
    if (value) return value;
  } catch {}
  return (process.env.CODEX_API_KEY || "").trim();
}

function describe(line) {
  try {
    const message = JSON.parse(line);
    if (message === null || typeof message !== "object") return { kind: "other" };
    const id = Object.hasOwn(message, "id") ? message.id : undefined;
    if (typeof message.method === "string") {
      return { kind: id === undefined ? "notification" : "request", method: message.method.slice(0, 80), id };
    }
    return { kind: Object.hasOwn(message, "error") ? "error" : "response", id };
  } catch {
    return { kind: "invalid" };
  }
}

// Splits a byte stream into complete lines, forwarding each unchanged. Returns false on an oversize line.
function lineRelay(onLine) {
  let pending = [];
  let pendingBytes = 0;
  return (chunk) => {
    let start = 0;
    for (let index = chunk.indexOf(10); index !== -1; index = chunk.indexOf(10, start)) {
      const piece = chunk.subarray(start, index + 1);
      if (pendingBytes + piece.length > MAX_LINE_BYTES + 1) return false;
      pending.push(piece);
      onLine(Buffer.concat(pending));
      pending = [];
      pendingBytes = 0;
      start = index + 1;
    }
    if (start < chunk.length) {
      const rest = chunk.subarray(start);
      pendingBytes += rest.length;
      if (pendingBytes > MAX_LINE_BYTES) return false;
      pending.push(Buffer.from(rest));
    }
    return true;
  };
}

function waitForExit(child, ms) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), ms);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve(true);
    });
  });
}

function signalGroup(pid, signal) {
  try {
    process.kill(-pid, signal);
    return true;
  } catch {
    return false;
  }
}

function refuseBusy(socket) {
  let buffered = Buffer.alloc(0);
  const timer = setTimeout(() => socket.destroy(), BUSY_FIRST_LINE_MS);
  socket.on("error", () => {});
  socket.on("data", (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    const end = buffered.indexOf(10);
    if (end === -1 && buffered.length <= MAX_LINE_BYTES) return;
    clearTimeout(timer);
    const { id } = end === -1 ? {} : describe(buffered.subarray(0, end).toString("utf8"));
    if (id !== undefined) {
      const error = { code: -32000, message: `Coding-agent worker is busy: ${MAX_CONNECTIONS} connections in use` };
      socket.end(`${JSON.stringify({ jsonrpc: "2.0", id, error })}\n`);
    } else {
      socket.destroy();
    }
  });
}

function serve(socket) {
  if (active.size >= MAX_CONNECTIONS) {
    log({ event: "busy", active: active.size });
    refuseBusy(socket);
    return;
  }
  const conn = ++counter;
  const dir = fs.mkdtempSync(path.join(STATE_ROOT, `conn-${conn}-`));
  const codexHome = path.join(dir, "codex-home");
  fs.mkdirSync(codexHome, { mode: 0o700 });
  if (fs.existsSync(CONFIG_TEMPLATE)) {
    fs.copyFileSync(CONFIG_TEMPLATE, path.join(codexHome, "config.toml"));
    fs.chmodSync(path.join(codexHome, "config.toml"), 0o600);
  } else if (DEFAULT_MODEL_OK) {
    fs.writeFileSync(path.join(codexHome, "config.toml"), `model = "${DEFAULT_MODEL}"\n`, { mode: 0o600 });
  }
  const secret = readSecret();
  const env = { PATH: "/usr/local/bin:/usr/bin:/bin", HOME: dir, CODEX_HOME: codexHome };
  for (const name of PASS_ENV) if (process.env[name]) env[name] = process.env[name];
  if (secret) env.CODEX_API_KEY = secret;
  const child = spawn(ADAPTER[0], ADAPTER.slice(1), { cwd: dir, env, detached: true, stdio: ["pipe", "pipe", "pipe"] });
  const record = { child, socket, dir, closing: null };
  active.set(conn, record);
  log({ event: "accept", conn, pid: child.pid, active: active.size, api_key_provisioned: Boolean(secret),
        config_template: fs.existsSync(path.join(codexHome, "config.toml")) });

  const redact = (text) => (secret ? text.split(secret).join("[redacted]") : text);
  const close = (reason) => {
    if (!record.closing) record.closing = cleanup(conn, record, reason);
    return record.closing;
  };

  const toAgent = lineRelay((line) => {
    log({ event: "message", conn, dir: "to_agent", bytes: line.length, ...describe(line.toString("utf8")) });
    if (!child.stdin.destroyed) child.stdin.write(line);
  });
  const toClient = lineRelay((line) => {
    log({ event: "message", conn, dir: "to_client", bytes: line.length, ...describe(line.toString("utf8")) });
    if (!socket.destroyed) socket.write(line);
  });
  socket.on("data", (chunk) => {
    if (!toAgent(chunk)) close("oversize message from client");
  });
  child.stdout.on("data", (chunk) => {
    if (!toClient(chunk)) close("oversize message from agent");
  });
  let stderrTail = "";
  child.stderr.on("data", (chunk) => {
    stderrTail += chunk.toString("utf8");
    const lines = stderrTail.split("\n");
    stderrTail = lines.pop().slice(-4000);
    for (const line of lines) {
      if (line.trim()) log({ event: "adapter_stderr", conn, line: redact(line).slice(0, 500) });
    }
  });
  socket.on("end", () => close("client closed the connection"));
  socket.on("error", (error) => close(`socket error ${error.code || error.name}`));
  socket.on("close", () => close("socket closed"));
  child.on("exit", (code, signal) => {
    log({ event: "adapter_exit", conn, code, signal });
    close("adapter exited");
  });
  child.on("error", (error) => close(`adapter failed to start: ${error.code || error.name}`));
  child.stdin.on("error", () => {});
}

async function cleanup(conn, record, reason) {
  const { child, socket, dir } = record;
  const started = Date.now();
  socket.destroy();
  try {
    child.stdin.end();
  } catch {}
  let stage = "graceful";
  let exited = await waitForExit(child, GRACEFUL_MS);
  if (!exited) {
    stage = "term";
    signalGroup(child.pid, "SIGTERM");
    exited = await waitForExit(child, TERM_MS);
  }
  if (!exited) {
    stage = "kill";
    signalGroup(child.pid, "SIGKILL");
    exited = await waitForExit(child, KILL_MS);
  }
  // The adapter's own children share its process group; none may outlive the connection.
  const leftovers = signalGroup(child.pid, "SIGKILL");
  fs.rmSync(dir, { recursive: true, force: true });
  active.delete(conn);
  log({ event: "cleanup", conn, reason, stage, exited, group_leftovers_killed: leftovers,
        exit_code: child.exitCode, signal: child.signalCode, ms: Date.now() - started, active: active.size });
}

function removeStaleSocket() {
  try {
    if (fs.lstatSync(SOCKET).isSocket()) fs.unlinkSync(SOCKET);
  } catch {}
}

removeStaleSocket();
const server = net.createServer({ allowHalfOpen: false }, serve);
server.on("error", (error) => {
  log({ event: "server_error", code: error.code || error.name });
  process.exit(1);
});
server.listen(SOCKET, () => {
  fs.chmodSync(SOCKET, 0o600);
  log({ event: "listening", socket: SOCKET, uid: process.getuid(), gid: process.getgid(),
        max_connections: MAX_CONNECTIONS, api_key_provisioned: Boolean(readSecret()),
        config_template: fs.existsSync(CONFIG_TEMPLATE), default_model: DEFAULT_MODEL_OK ? DEFAULT_MODEL : null,
        default_model_refused: DEFAULT_MODEL !== "" && !DEFAULT_MODEL_OK });
});

async function shutdown(signal) {
  log({ event: "shutdown", signal, active: active.size });
  server.close();
  await Promise.all([...active.entries()].map(([conn, record]) => record.closing
    || (record.closing = cleanup(conn, record, "worker shutdown"))));
  removeStaleSocket();
  process.exit(0);
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
