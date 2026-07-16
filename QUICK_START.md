# PulseCodeAI IDE - Quick Start Guide

## 🚀 Getting Started in 5 Minutes

### Prerequisites
- Windows 10/11
- Node.js v24.18.0+
- pnpm v11.5.3+
- Visual Studio 2022 Community (with C++ workload)
- Python 3.15.0+
- Git

### Step-by-Step

1. **Open Terminal** (Powershell or Command Prompt) and navigate to the `desktop` workspace:
   ```powershell
   cd d:\vscode_src\desktop
   ```

2. **Launch the IDE**:
   ```powershell
   .\scripts\code.bat
   ```

### Using the Pulse Agent
- Look for the **Pulse Agent icon** in the Activity Bar (sidebar)
- Click to open the agent view
- Use `Ctrl+Shift+P` → "Pulse Agent: Ask..." to start chatting
- View logs in **View → Output → Pulse Agent**

### Troubleshooting Quick Fixes
| Issue | Solution |
|-------|----------|
| Native module `MODULE_NOT_FOUND` errors | Rebuild with: `.\node_modules\.bin\electron-rebuild.cmd -v 42.6.0` |
| "Cannot find module 'undici-types'" | Run: `pnpm add -D @types/node` |
| `postinstall.ts` fails to find instructions | Verify `.github/copilot-instructions.md` exists |

### Verification
Success looks like:
- IDE window titled "PulseCodeAI IDE"
- Pulse Agent icon in Activity Bar
- Able to open agent view and chat with AI
- Backend logs in "Pulse Agent" output channel

---

**You're now ready to develop with AI assistance!**  
For full details, see PULSECODEAI_USER_GUIDE.md