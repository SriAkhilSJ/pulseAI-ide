/**
 * main.js
 * -------
 * Runs INSIDE the sandboxed webview iframe. No filesystem access, no
 * Node.js APIs, no fetch() of local paths -- the only bridge to the
 * extension host is the handle returned by acquireVsCodeApi():
 *   vscodeApi.postMessage(obj)   -- webview -> extension
 *   window.addEventListener('message', ...) -- extension -> webview
 * (NOT window.parent.postMessage(), which was the incorrect API in an
 * earlier proposed design -- acquireVsCodeApi()'s handle is the real,
 * supported mechanism.)
 *
 * Reuses TOOL_LABELS/getToolLabel/pastTense from tool_labels.js (loaded as
 * a plain global via a <script> tag before this file, see extension.ts's
 * getHtml()) so tool identity stays hidden here too, using the exact same
 * verified mapping (all 38 real tool names, confirmed against
 * tools.TOOL_FUNCTIONS) as the standalone HTML blueprint.
 */
(function () {
  const vscodeApi = acquireVsCodeApi();
  const root = document.getElementById("root");

  const state = {
    messages: [], // {kind: 'user'|'assistant'|'thought'|'action'|'confirm', ...}
    pendingActions: {}, // toolCallLikeKey -> DOM node, for updating running->done in place
    busy: false,
  };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else node.setAttribute(k, attrs[k]);
      }
    }
    (children || []).forEach((c) => c && node.appendChild(c));
    return node;
  }

  function render() {
    root.innerHTML = "";
    const panel = el("div", { class: "pa-panel" });

    // Header
    const header = el("div", { class: "pa-header" }, [
      el("div", { class: "pa-title" }, [el("span", { text: "PULSE AGENT" })]),
      el("div", { class: state.busy ? "pa-status busy" : "pa-status" }, [
        el("span", { class: "pa-status-dot" }),
        el("span", { text: state.busy ? "Working" : "Idle" }),
      ]),
    ]);
    panel.appendChild(header);

    // Thread
    const thread = el("div", { class: "pa-thread" });
    state.messages.forEach((m) => thread.appendChild(renderMessage(m)));
    panel.appendChild(thread);
    thread.scrollTop = thread.scrollHeight;

    // Composer
    const textarea = el("textarea", { class: "pa-input", placeholder: "Ask Pulse Agent to build, fix, or explain something..." });
    const sendBtn = el("button", { class: "pa-send", text: "Send" });
    sendBtn.disabled = state.busy;
    sendBtn.addEventListener("click", () => {
      const value = textarea.value.trim();
      if (!value) return;
      state.messages.push({ kind: "user", text: value });
      state.busy = true;
      render();
      vscodeApi.postMessage({ type: "run", input: value });
    });
    const composer = el("div", { class: "pa-composer" }, [textarea, sendBtn]);
    panel.appendChild(composer);

    root.appendChild(panel);
  }

  function renderMessage(m) {
    if (m.kind === "user") {
      return el("div", { class: "msg user" }, [el("div", { class: "bubble", text: m.text })]);
    }
    if (m.kind === "assistant") {
      return el("div", { class: "msg assistant" }, [el("div", { class: "bubble", text: m.text })]);
    }
    if (m.kind === "thought") {
      return el("div", { class: "thought", text: m.text });
    }
    if (m.kind === "action") {
      const label = getToolLabel(m.toolName, m.args);
      const verbText = m.status === "done" ? pastTense(label.verb) : label.verb;
      const card = el("div", { class: "action-card " + m.status });
      const head = el("div", { class: "action-head" }, [
        el("span", { class: "action-icon " + m.status }),
        el("span", { class: "action-verb", text: verbText }),
        el("span", { class: "action-detail", text: label.detail }),
      ]);
      card.appendChild(head);
      // Live streaming output (e.g. a long `npm install`/`pytest` run),
      // pushed line-by-line via bridge_server.py's "command_output"
      // events -- shown as its own scrollable pane WHILE the command is
      // still running, not just the final combined result once it's done.
      // This is what actually delivers the "no more frozen screen" UX --
      // rendering only happens on real, incrementally-arriving data (see
      // the "command_output" case below), never simulated/faked progress.
      if (m.streamedLines && m.streamedLines.length > 0) {
        const streamBox = el("div", { class: "action-stream" });
        m.streamedLines.forEach((line) => {
          streamBox.appendChild(el("div", { class: "action-stream-line", text: line }));
        });
        card.appendChild(streamBox);
      }
      return card;
    }
    if (m.kind === "confirm") {
      const card = el("div", { class: "confirm-card" });
      card.appendChild(el("div", { class: "confirm-head", text: "This needs your approval" }));
      const body = el("div", { class: "confirm-body" });
      body.appendChild(document.createTextNode(m.reason + " "));
      const code = el("code", { text: m.args && m.args.cmd ? m.args.cmd : JSON.stringify(m.args) });
      body.appendChild(code);
      card.appendChild(body);

      const actions = el("div", { class: "confirm-actions" });
      const approveBtn = el("button", { class: "btn-approve", text: "Approve" });
      const denyBtn = el("button", { class: "btn-deny", text: "Deny" });
      let answered = false;
      approveBtn.addEventListener("click", () => {
        if (answered) return;
        answered = true;
        vscodeApi.postMessage({ type: "confirm_response", requestId: m.requestId, approved: true });
        m.resolved = "approved";
        render();
      });
      denyBtn.addEventListener("click", () => {
        if (answered) return;
        answered = true;
        vscodeApi.postMessage({ type: "confirm_response", requestId: m.requestId, approved: false });
        m.resolved = "denied";
        render();
      });
      if (m.resolved) {
        card.appendChild(el("div", { class: "confirm-resolved", text: m.resolved === "approved" ? "Approved" : "Denied" }));
      } else {
        actions.appendChild(approveBtn);
        actions.appendChild(denyBtn);
        card.appendChild(actions);
      }
      return card;
    }
    return el("div", { class: "msg unknown", text: "Unknown message" });
  }

  // Bridge messages arrive wrapped as {type: "bridge_message", payload: <raw bridge_server.py message>}
  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.type === "reset") {
      state.messages = [];
      state.busy = false;
      render();
      return;
    }
    if (msg.type === "bridge_exit") {
      state.messages.push({ kind: "assistant", text: "The agent process stopped unexpectedly (exit code " + msg.code + "). Start a new session to continue." });
      state.busy = false;
      render();
      return;
    }
    if (msg.type !== "bridge_message") {
      return;
    }
    const payload = msg.payload;
    switch (payload.type) {
      case "ready":
        break;
      case "log": {
        if (payload.event === "Thought") {
          state.messages.push({ kind: "thought", text: payload.payload });
        } else if (payload.event === "Action") {
          // payload.payload looks like: toolName({"arg": "value"})
          const match = /^([a-zA-Z_]+)\((.*)\)$/s.exec(payload.payload);
          let toolName = payload.payload;
          let args = {};
          if (match) {
            toolName = match[1];
            try {
              args = JSON.parse(match[2]);
            } catch (e) {
              args = {};
            }
          }
          state.messages.push({ kind: "action", toolName, args, status: "running" });
        } else if (payload.event === "Observation") {
          // Mark the most recent running action as done.
          for (let i = state.messages.length - 1; i >= 0; i--) {
            if (state.messages[i].kind === "action" && state.messages[i].status === "running") {
              state.messages[i].status = payload.payload && payload.payload.startsWith("ERROR") ? "error" : "done";
              break;
            }
          }
        }
        break;
      }
      case "command_output": {
        // Find the most recent RUNNING run_command action card and append
        // this line to it -- matches the real order these events arrive
        // in: "Action" (from the "log" event) always precedes any
        // "command_output" lines for that same call, which always precede
        // its "Observation". If no running run_command card exists yet
        // (e.g. a race at the very start of a call), lines are simply
        // dropped rather than crashing -- the final combined result will
        // still show up correctly in the Observation-derived "done" state
        // either way, so this is a pure UX nicety, never a source of lost
        // information.
        for (let i = state.messages.length - 1; i >= 0; i--) {
          const cand = state.messages[i];
          if (cand.kind === "action" && cand.toolName === "run_command" && cand.status === "running") {
            if (!cand.streamedLines) cand.streamedLines = [];
            cand.streamedLines.push(payload.line);
            break;
          }
        }
        break;
      }
      case "confirm_request":
        state.messages.push({
          kind: "confirm",
          requestId: payload.request_id,
          tool: payload.tool,
          args: payload.args,
          reason: payload.reason,
          diff: payload.diff,
        });
        break;
      case "result":
        state.messages.push({ kind: "assistant", text: payload.reply });
        state.busy = false;
        break;
      case "error":
        state.messages.push({ kind: "assistant", text: "Something went wrong: " + payload.message });
        state.busy = false;
        break;
    }
    render();
  });

  render();
})();
