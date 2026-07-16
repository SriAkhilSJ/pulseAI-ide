/*
app.js
------
PulseAI Studio — Client-Side Multi-Agent Controller & Interactive Mission Stepper.
Simulates real-time ReAct loop stepping, token counter updates, and FTS5 memory search.
*/

class PulseStudioController {
    constructor() {
        this.tokenCount = 1420;
        this.activeTurn = 1;
        this.isMissionRunning = false;
        this.initEventListeners();
    }

    initEventListeners() {
        document.getElementById('dispatch-btn').addEventListener('click', () => this.dispatchMission());
        document.getElementById('prompt-input').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.dispatchMission();
        });
        document.getElementById('reset-pipeline-btn').addEventListener('click', () => this.resetPipeline());
        document.getElementById('search-memory-btn').addEventListener('click', () => this.searchMemory());
        document.getElementById('memory-query').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.searchMemory();
        });
    }

    addLogEntry(role, message) {
        const viewport = document.getElementById('console-viewport');
        const now = new Date().toLocaleTimeString();
        
        const entry = document.createElement('div');
        entry.className = `log-entry ${role}`;
        
        const timeSpan = document.createElement('span');
        timeSpan.className = 'timestamp';
        timeSpan.innerText = `[${now}]`;
        
        const msgSpan = document.createElement('span');
        msgSpan.className = 'msg';
        msgSpan.innerHTML = message;
        
        entry.appendChild(timeSpan);
        entry.appendChild(msgSpan);
        viewport.appendChild(entry);
        viewport.scrollTop = viewport.scrollHeight;
    }

    updateTokenCounter(delta) {
        this.tokenCount += delta;
        document.getElementById('token-count').innerText = `${this.tokenCount.toLocaleString()} / 128,000`;
    }

    setAgentCardStatus(role, status, statusText) {
        const card = document.querySelector(`.agent-card[data-role="${role}"]`);
        if (!card) return;
        
        card.className = `agent-card ${status}`;
        const tag = card.querySelector('.status-tag');
        if (tag) {
            tag.innerText = statusText;
            tag.className = `status-tag ${status === 'active' ? 'pulse' : ''}`;
        }
    }

    async dispatchMission() {
        const input = document.getElementById('prompt-input');
        const prompt = input.value.trim() || "@pulse /feature Build ultra-fast ripgrep codebase search engine with AST validation.";
        input.value = "";
        
        if (this.isMissionRunning) {
            this.addLogEntry("system", "⚠️ Mission already active. Please wait or click Reset.");
            return;
        }

        this.isMissionRunning = true;
        this.addLogEntry("user", prompt);
        this.updateTokenCounter(42);

        // Step 1: Planner Agent
        this.setAgentCardStatus("Planner", "active", "RUNNING");
        this.addLogEntry("assistant", "<strong>[PlannerAgent] Thought:</strong> I will inspect existing AST and search tools before writing the execution plan (`PLAN.md`).");
        await this.sleep(800);
        this.addLogEntry("tool-call", "🛠️ <strong>Tool Call:</strong> <code>grep_files(pattern=\"class GrepFilesTool\")</code>");
        await this.sleep(600);
        this.addLogEntry("tool-obs", "✓ <strong>Observation:</strong> Found class in `packages/tools/git/src/git_tools.py:80`. Plan verified safe.");
        this.setAgentCardStatus("Planner", "completed", "DONE");
        this.updateTokenCounter(180);

        // Step 2: Coder Agent
        this.setAgentCardStatus("Coder", "active", "RUNNING");
        this.addLogEntry("assistant", "<strong>[CoderAgent] Thought:</strong> Executing AST modification to insert ripgrep optimization.");
        await this.sleep(800);
        this.addLogEntry("tool-call", "🛠️ <strong>Tool Call:</strong> <code>filesystem_write_file(path=\"packages/tools/search/src/fast_search.py\")</code>");
        await this.sleep(700);
        this.addLogEntry("tool-obs", "✓ <strong>Observation:</strong> File written. Running `lsp_get_diagnostics`...");
        await this.sleep(500);
        this.addLogEntry("tool-obs", "✓ <strong>Observation:</strong> `lsp_get_diagnostics` returned 0 syntax errors (`GREEN`).");
        this.setAgentCardStatus("Coder", "completed", "DONE");
        this.updateTokenCounter(210);

        // Step 3: Reviewer Agent
        this.setAgentCardStatus("Reviewer", "active", "RUNNING");
        this.addLogEntry("assistant", "<strong>[ReviewerAgent] Thought:</strong> Checking git diff for path traversal vulnerabilities and null checks.");
        await this.sleep(700);
        this.addLogEntry("tool-call", "🛠️ <strong>Tool Call:</strong> <code>git_diff(path=\"packages/tools/search/\")</code>");
        await this.sleep(600);
        this.addLogEntry("tool-obs", "✓ <strong>Observation:</strong> `PathGuard.assert_safe_path` verified present on file inputs. Zero security risks.");
        this.setAgentCardStatus("Reviewer", "completed", "DONE");
        this.updateTokenCounter(150);

        // Step 4: Tester & Debugger Agent
        this.setAgentCardStatus("Tester", "active", "RUNNING");
        this.addLogEntry("assistant", "<strong>[TesterAgent] Thought:</strong> Executing automated verification (`RED -> GREEN -> REFACTOR`).");
        await this.sleep(800);
        this.addLogEntry("tool-call", "🛠️ <strong>Tool Call:</strong> <code>run_command(command=\"pytest packages/tools/search/test/ -v\")</code>");
        await this.sleep(900);
        this.addLogEntry("tool-obs", "✓ <strong>Observation:</strong> 4 passed in 0.03s (`GREEN VERIFIED`).");
        this.setAgentCardStatus("Tester", "completed", "DONE");
        this.updateTokenCounter(90);

        this.addLogEntry("assistant", "✅ <strong>Mission Complete:</strong> All 4 specialized agents executed their sandboxed tools with zero security violations and 100% test passing.");
        this.isMissionRunning = false;
    }

    resetPipeline() {
        this.isMissionRunning = false;
        ["Planner", "Coder", "Reviewer", "Tester"].forEach(role => {
            this.setAgentCardStatus(role, "pending", "WAITING");
        });
        document.getElementById('console-viewport').innerHTML = `
            <div class="log-entry system">
                <span class="timestamp">[${new Date().toLocaleTimeString()}]</span>
                <span class="msg">Pipeline reset. Ready for new autonomous agent dispatch.</span>
            </div>
        `;
        this.tokenCount = 1420;
        document.getElementById('token-count').innerText = "1,420 / 128,000";
    }

    searchMemory() {
        const input = document.getElementById('memory-query');
        const query = input.value.trim().toLowerCase() || "jwt";
        const container = document.getElementById('memory-results');
        
        container.innerHTML = "";
        
        if (query.includes("jwt") || query.includes("auth")) {
            container.innerHTML += `
                <div class="note-card">
                    <span class="note-tag auth">AUTH DECISION</span>
                    <p>Migrated session validation to JWT tokens with 15-minute expiry across distributed agent sessions.</p>
                    <span class="note-time">Session ID: mission-session-042 (Score: 0.98)</span>
                </div>
            `;
        }
        if (query.includes("db") || query.includes("wal") || query.includes("sqlite") || query.includes("pool")) {
            container.innerHTML += `
                <div class="note-card">
                    <span class="note-tag db">DATABASE ARCHITECTURE</span>
                    <p>Configured SQLite WAL mode (\`PRAGMA journal_mode=WAL;\`) inside ConversationMemory to support concurrent parallel agent writes.</p>
                    <span class="note-time">Session ID: mission-session-044 (Score: 0.95)</span>
                </div>
            `;
        }
        if (container.innerHTML === "") {
            container.innerHTML = `
                <div class="note-card">
                    <p>No historical memory matches found for "${query}".</p>
                </div>
            `;
        }
    }

    sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
}

// Initialize on load
window.addEventListener('DOMContentLoaded', () => {
    window.studioController = new PulseStudioController();
});
