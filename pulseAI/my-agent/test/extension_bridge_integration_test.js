/**
 * extension_bridge_integration_test.js
 * -------------------------------------
 * A real, live test that exercises the SAME spawn/readline/JSON-parsing
 * logic extension.ts uses to talk to bridge_server.py -- without needing
 * a full VS Code test harness (not installable in this sandbox). This
 * doesn't mock bridge_server.py; it spawns the real Python process and
 * sends/receives real JSON lines over real stdio, using plain Node.js
 * (child_process + readline), mirroring extension.ts's startBridge()
 * function line for line.
 *
 * Run with: node test/extension_bridge_integration_test.js
 * (run from the my-agent/ directory, or set AGENT_ROOT env var)
 */
const { spawn } = require("child_process");
const readline = require("readline");
const path = require("path");
const fs = require("fs");

const AGENT_ROOT = process.env.AGENT_ROOT || path.resolve(__dirname, "..");

function startBridge() {
  const proc = spawn("python3", ["-u", path.join(AGENT_ROOT, "bridge_server.py")], {
    cwd: AGENT_ROOT,
    env: { ...process.env, PYTHONPATH: AGENT_ROOT },
  });
  return proc;
}

function collectMessages(proc, messages) {
  const rl = readline.createInterface({ input: proc.stdout });
  rl.on("line", (line) => {
    if (!line.trim()) return;
    try {
      messages.push(JSON.parse(line));
    } catch (e) {
      console.log("NON-JSON LINE (would be silently dropped by extension.ts, same as here):", line);
    }
  });
  proc.stderr.on("data", (d) => {
    // Same as extension.ts's outputChannel.appendLine -- just observe, don't fail on it.
  });
}

function waitFor(messages, predicate, timeoutMs) {
  return new Promise((resolve) => {
    const start = Date.now();
    const interval = setInterval(() => {
      const found = messages.find(predicate);
      if (found) {
        clearInterval(interval);
        resolve(found);
      } else if (Date.now() - start > timeoutMs) {
        clearInterval(interval);
        resolve(null);
      }
    }, 100);
  });
}

async function testReadyAndSimpleRun() {
  const messages = [];
  const proc = startBridge();
  collectMessages(proc, messages);

  const ready = await waitFor(messages, (m) => m.type === "ready", 10000);
  if (!ready) throw new Error("expected a ready message");
  console.log("PASS: bridge_server emits ready, observed via real Node.js child_process/readline (same code path as extension.ts)");

  proc.stdin.write(JSON.stringify({ type: "run", id: "nodetest1", input: "Say the word 'pong' and nothing else." }) + "\n");

  const result = await waitFor(messages, (m) => m.type === "result" && m.id === "nodetest1", 90000);
  if (!result) throw new Error("expected a result message");
  console.log("result reply:", result.reply);
  if (!result.reply.toLowerCase().includes("pong")) {
    throw new Error("expected reply to contain 'pong'");
  }
  console.log("PASS: real run request produces a real result, parsed identically to how extension.ts's readline handler would");

  proc.kill();
}

async function testConfirmFlowFromNodeSide() {
  const markerDir = path.join(AGENT_ROOT, "test", "scratch_node_bridge_marker");
  if (fs.existsSync(markerDir)) fs.rmSync(markerDir, { recursive: true });
  fs.mkdirSync(markerDir);
  fs.writeFileSync(path.join(markerDir, "f.txt"), "delete only after approval\n");

  const messages = [];
  const proc = startBridge();
  collectMessages(proc, messages);

  await waitFor(messages, (m) => m.type === "ready", 10000);

  proc.stdin.write(
    JSON.stringify({
      type: "run",
      id: "nodetest2",
      input:
        "Call the run_command tool directly with cmd='rm -rf test/scratch_node_bridge_marker'. Do NOT ask me in your reply first -- just call the tool now; the system itself will pause and ask for real confirmation automatically.",
    }) + "\n"
  );

  const confirmReq = await waitFor(messages, (m) => m.type === "confirm_request", 90000);
  if (!confirmReq) throw new Error("expected a confirm_request message");
  console.log("confirm_request (exact shape extension.ts's webview would receive):", JSON.stringify(confirmReq));
  if (confirmReq.tool !== "run_command") throw new Error("expected tool to be run_command");
  console.log("PASS: confirm_request received via real Node.js child_process, matching extension.ts's expected shape");

  if (!fs.existsSync(markerDir)) {
    throw new Error("marker dir was deleted before approval was sent -- the gate did not actually hold");
  }
  console.log("PASS: destructive command genuinely paused (marker directory still exists)");

  proc.stdin.write(JSON.stringify({ type: "confirm_response", request_id: confirmReq.request_id, approved: true }) + "\n");

  const result = await waitFor(messages, (m) => m.type === "result" && m.id === "nodetest2", 30000);
  if (!result) throw new Error("expected a result after approving");

  await new Promise((r) => setTimeout(r, 500));
  if (fs.existsSync(markerDir)) {
    throw new Error("marker dir should have been deleted after approval");
  }
  console.log("PASS: after sending confirm_response from the Node.js side, the destructive command actually ran");

  proc.kill();
}

(async () => {
  await testReadyAndSimpleRun();
  await testConfirmFlowFromNodeSide();
  console.log("\nALL TESTS PASSED");
})().catch((e) => {
  console.error("TEST FAILED:", e);
  process.exit(1);
});
