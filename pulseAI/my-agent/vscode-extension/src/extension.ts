/**
 * extension.ts
 * ------------
 * VS Code extension host code. This is a normal, UNSANDBOXED Node.js
 * process -- it has full filesystem access and can spawn child processes,
 * unlike the webview (see PulseAgentViewProvider below for the sandboxed
 * side). This file:
 *   1. Spawns bridge_server.py as a child process and talks to it over
 *      stdin/stdout, one JSON object per line (see bridge_server.py's
 *      module docstring for the exact wire format -- this is NOT ACP's
 *      JSON-RPC 2.0 framing, deliberately: that protocol was never
 *      fact-checked in this project, so the bridge only uses
 *      infrastructure that has already been built and tested:
 *      agent.run_agent(), confirm_bridge.webview_confirm(), tool_labels.js).
 *   2. Registers the webview view provider and relays messages between
 *      the bridge process and the webview using VS Code's REAL message
 *      API (webview.postMessage() / webview.onDidReceiveMessage()) --
 *      NOT window.parent.postMessage() and NOT fetch() of a local path,
 *      both of which were confirmed incorrect for a sandboxed webview in
 *      earlier design review (see design/webview_flat.html's dev notes
 *      and README.md's "Corrected assumptions about VS Code webview
 *      integration" section).
 */
import * as vscode from "vscode";
import { spawn, ChildProcessWithoutNullStreams } from "child_process";
import * as path from "path";
import * as readline from "readline";

interface ToolLabelEntry {
  verb: string;
  icon: string;
  detail: (args: Record<string, unknown>) => string;
}

let bridgeProcess: ChildProcessWithoutNullStreams | null = null;
let currentProvider: PulseAgentViewProvider | null = null;

function getPythonPath(): string {
  const config = vscode.workspace.getConfiguration("pulseAgent");
  return config.get<string>("pythonPath", "python3");
}

function getAgentRoot(): string {
  const config = vscode.workspace.getConfiguration("pulseAgent");
  const configured = config.get<string>("agentRoot", "");
  if (configured) {
    return configured;
  }
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length > 0 ? folders[0].uri.fsPath : process.cwd();
}

function startBridge(context: vscode.ExtensionContext): ChildProcessWithoutNullStreams {
  const agentRoot = getAgentRoot();
  const scriptPath = path.join(agentRoot, "bridge_server.py");
  const pythonPath = getPythonPath();

  const proc = spawn(pythonPath, ["-u", scriptPath], {
    cwd: agentRoot,
    env: { ...process.env, PYTHONPATH: agentRoot },
  });

  const rl = readline.createInterface({ input: proc.stdout });
  rl.on("line", (line: string) => {
    if (!line.trim()) {
      return;
    }
    let msg: any;
    try {
      msg = JSON.parse(line);
    } catch {
      // Non-JSON stdout noise (shouldn't happen -- bridge_server.py only
      // ever writes one JSON object per line -- but never crash the
      // extension over a malformed/partial line).
      return;
    }
    currentProvider?.handleBridgeMessage(msg);
  });

  proc.stderr.on("data", (data: Buffer) => {
    // LiteLLM/provider warnings and Python tracebacks land here -- surface
    // them to the Output channel rather than silently swallowing, but
    // never crash the extension over stderr noise.
    outputChannel.appendLine(`[bridge stderr] ${data.toString()}`);
  });

  proc.on("exit", (code: number | null) => {
    outputChannel.appendLine(`[bridge] process exited with code ${code}`);
    currentProvider?.handleBridgeExit(code);
    bridgeProcess = null;
  });

  return proc;
}

let outputChannel: vscode.OutputChannel;

export function activate(context: vscode.ExtensionContext) {
  outputChannel = vscode.window.createOutputChannel("Pulse Agent");

  const provider = new PulseAgentViewProvider(context.extensionUri, () => {
    if (!bridgeProcess) {
      bridgeProcess = startBridge(context);
    }
    return bridgeProcess;
  });
  currentProvider = provider;

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("pulseAgentView", provider, {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("pulseAgent.run", async () => {
      const input = await vscode.window.showInputBox({ prompt: "What should Pulse Agent do?" });
      if (input) {
        provider.sendRun(input);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("pulseAgent.newSession", () => {
      provider.resetSession();
    })
  );
}

export function deactivate() {
  if (bridgeProcess && !bridgeProcess.killed) {
    bridgeProcess.kill();
  }
}

/**
 * The webview side. Everything inside webview.html's <script> runs in a
 * SANDBOXED iframe with NO filesystem access and NO ability to spawn
 * processes or fetch() arbitrary local paths -- the only way in/out is
 * webview.postMessage()/onDidReceiveMessage() (extension -> webview) and
 * the handle from acquireVsCodeApi() (webview -> extension). This class
 * is the bridge between that sandboxed world and the real bridge_server.py
 * child process running in the (unsandboxed) extension host.
 */
class PulseAgentViewProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly ensureBridge: () => ChildProcessWithoutNullStreams
  ) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
    };
    webviewView.webview.html = this.getHtml(webviewView.webview);

    webviewView.webview.onDidReceiveMessage((message: any) => {
      switch (message.type) {
        case "run":
          this.sendRun(message.input, message.missionId);
          break;
        case "confirm_response":
          this.sendConfirmResponse(message.requestId, message.approved);
          break;
        case "ready":
          // Webview finished mounting -- nothing to do yet, but a real
          // extension could replay recent history here.
          break;
      }
    });
  }

  sendRun(input: string, missionId?: string): void {
    const proc = this.ensureBridge();
    const id = `req_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const wireMsg: Record<string, unknown> = { type: "run", id, input };
    if (missionId) {
      wireMsg.mission_id = missionId;
    }
    proc.stdin.write(JSON.stringify(wireMsg) + "\n");
    this.view?.webview.postMessage({ type: "run_started", id });
  }

  sendConfirmResponse(requestId: string, approved: boolean): void {
    const proc = this.ensureBridge();
    proc.stdin.write(JSON.stringify({ type: "confirm_response", request_id: requestId, approved }) + "\n");
  }

  resetSession(): void {
    this.view?.webview.postMessage({ type: "reset" });
  }

  handleBridgeMessage(msg: any): void {
    if (!this.view) {
      return;
    }
    // Forward bridge_server.py's messages straight through to the
    // webview -- the webview-side JS (media/main.js) is responsible for
    // interpreting {type: "log"|"confirm_request"|"result"|"error"|"ready"}
    // and rendering the friendly (non-tool-name-leaking) UI, using the
    // SAME tool_labels.js mapping already built and verified against the
    // real tool registry (see design/tool_labels.js).
    this.view.webview.postMessage({ type: "bridge_message", payload: msg });
  }

  handleBridgeExit(code: number | null): void {
    this.view?.webview.postMessage({ type: "bridge_exit", code });
  }

  private getHtml(webview: vscode.Webview): string {
    const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "main.js"));
    const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "main.css"));
    const labelsUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "tool_labels.js"));
    const nonce = getNonce();

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';" />
  <link href="${styleUri}" rel="stylesheet" />
  <title>Pulse Agent</title>
</head>
<body>
  <div id="root"></div>
  <script nonce="${nonce}" src="${labelsUri}"></script>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}

function getNonce(): string {
  let text = "";
  const possible = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 32; i++) {
    text += possible.charAt(Math.floor(Math.random() * possible.length));
  }
  return text;
}
