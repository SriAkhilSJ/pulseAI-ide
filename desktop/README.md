# PulseCodeAI

This repository contains the source for PulseCodeAI, a VS Code OSS‑based IDE with integrated AI agents.

## Building and Running

### Prerequisites
- Node.js (v20+ recommended)
- pnpm (used for package management)
- Git

### Setup
```bash
# Clone the repository (if you haven't already)
git clone <repo-url>
cd <repo-folder>

# Install dependencies
pnpm install
```

### Build
```bash
# Compile TypeScript and bundle extensions
pnpm run compile
```

### Run
The IDE can be launched with the provided script.  
Because the optional native module `@vscode/policy-watcher` is not built in this repository, you must disable it via an environment variable:

```bash
# Using the batch file (Windows)
set VSCODE_POLICY_DISABLED=1
scripts\code.bat
```

Or, equivalently, launch the Electron binary directly:

```bash
set VSCODE_POLICY_DISABLED=1
.build\electron\PulseCodeAI.exe --disable-extension=vscode.vscode-api-tests .
```

### What was fixed
- **Policy watcher missing**: The `NativePolicyService` was patched to gracefully handle the absence of `@vscode/policy-watcher` when `VSCODE_POLICY_DISABLED` is set.
- **Batch file path issues**: The `scripts/code.bat` file was corrected to properly extract the `nameShort` from `product.json` and build the path to the Electron executable.

### Notes
- Other optional native modules (e.g., `spdlog`, `sqlite3`, `windows-mutex`, `native-keymap`) may still show “could not locate the bindings file” warnings. These are non‑fatal and do not prevent the editor from starting.
- For a fully featured build with all native modules, you would need to run the platform‑specific native build steps (outside the scope of this quick start).

Happy coding with PulseCodeAI!