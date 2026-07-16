# PulseCodeAI IDE - Complete User Guide

## 🚀 Quick Start
To get the PulseCodeAI IDE running immediately:

```powershell
# 1. Navigate to desktop directory
cd d:\vscode_src\desktop

# 2. Launch the IDE script
.\scripts\code.bat
```

## 📋 System Requirements
- **OS**: Windows 10/11
- **Node.js**: v24.18.0 (or >= 22.14)
- **Package Manager**: pnpm v11.5.3+
- **Visual Studio 2026/2022**: With "Desktop development with C++" workload
- **Python**: 3.15.0+ (via PATH; used by node-gyp)
- **Git**: For repository operations

## 🛠️ Rebuilding Native Dependencies
If native C++ components require manual recompilation (e.g. SQLite, policy-watcher), run the rebuilding tool:
```powershell
cd d:\vscode_src\desktop
.\node_modules\.bin\electron-rebuild.cmd -v 42.6.0
```

## ▶️ Running the IDE
Launch the application with:
```powershell
.\scripts\code.bat
```

### What to Expect When Running:
- IDE window opens titled **"PulseCodeAI IDE"** (custom branding)
- No module load exceptions or native ABI crashes.
- All processes stay active and fully integrated.

## 💡 Using the Pulse Agent
Once the IDE is running:
1. **Locate the Agent**: Look for the **Pulse Agent icon** in the Activity Bar (left sidebar)
2. **Open the View**: Click the icon to open the agent interface
3. **Interact with the Agent**:
   - Use command palette (`Ctrl+Shift+P`) → "Pulse Agent: Ask..." to start conversation
   - Type requests in chat interface and press Enter
4. **View Logs**: Monitor agent activity in **View → Output → Pulse Agent**

## 📁 Project Structure
- **Source Code**: `/d/vscode_src/desktop/` (VS Code OSS fork)
- **Pulse Agent Extension Source**: `/d/vscode_src/pulseAI/my-agent/vscode-extension/`
- **Built Output**: `/d/vscode_src/desktop/.output/` (after build)
- **Integrated Extension**: `/d/vscode_src/desktop/.builtInExtensions/PulseCodeAI.agent/`

## 🔧 Customization Details
### Product Branding (Modified in desktop/product.json):
- `nameShort`: "PulseCodeAI"
- `nameLong`: "PulseCodeAI IDE"
- `applicationName`: "pulse-code"
- Custom window titles, menus, and data folder names

### Pulse Agent Extension Features:
- Chat participant: `pulsecode.pulse-agent`
- Commands:
  - `pulseAgent.run`: "Pulse Agent: Ask..."
  - `pulseAgent.newSession`: "Pulse Agent: New Session"
- Configuration options:
  - `pulseAgent.pythonPath`: Path to Python interpreter (default: "python3")
  - `pulseAgent.agentRoot`: Path to agent project (defaults to workspace folder)

## 🐛 Troubleshooting Common Issues

### Build/Installation Problems
| Symptom | Solution |
|---------|----------|
| "Invalid C/C++ Compiler Toolchain" | Verify VS 2022 is installed with C++ workload; check environment variables point to correct VS path; ensure `vcvarsall.bat` exists |
| "Cannot find module 'undici-types'" | Run: `pnpm add -D undici-types` |
| "Sinon typing errors" | Run: `pnpm update @types/sinon` |
| EBUSY/resource locked errors | Kill node/electron processes via Task Manager or `taskkill /f /im node.exe` |

### Runtime Issues
| Symptom | Solution |
|---------|----------|
| IDE fails to launch or shows blank screen | Ensure no other IDE instances running; delete `.build` and `.output` folders then rebuild |
| Extension not appearing in sidebar | Confirm installation in `.builtInExtensions/PulseCodeAI.agent/`; verify `package.json` has `"activationEvents": ["*"]`; try "Reload Window" (`Ctrl+Shift+P`) |
| Agent unresponsive | Check "View → Output → Pulse Agent" for backend logs; verify Python interpreter path in settings |

## 👨‍💻 For Developers: Contributing & Advanced Topics

### Building the Pulse Agent Extension
```bash
cd /d/vscode_src/pulseAI/my-agent/vscode-extension
pnpm install          # Installs @types/node and @types/vscode
pnpm run compile      # or: tsc -p .
```

### Deploying Extension Updates
After modifying the extension:
```bash
cd /d/vscode_src
rm -rf desktop/.builtInExtensions/PulseCodeAI.agent
mkdir -p desktop/.builtInExtensions/PulseCodeAI.agent
cp -r pulseAI/my-agent/vscode-extension/* desktop/.builtInExtensions/PulseCodeAI.agent/
rm -rf desktop/.builtInExtensions/PulseCodeAI.agent/node_modules
```

## ✅ Verification of Success
Your setup is working correctly when:
1. IDE launches without errors (starts cleanly from `.\scripts\code.bat`)
2. Window title shows "PulseCodeAI IDE"
3. Pulse Agent icon visible in Activity Bar (sidebar)
4. Clicking agent icon opens functional chat interface
5. Using "Pulse Agent: Ask..." initiates conversation
6. "Pulse Agent" output channel shows backend communication logs

---

**You're now ready to build amazing things with AI-assisted development!**  
Your PulseCodeAI IDE with integrated Pulse Agent is configured and ready for use.