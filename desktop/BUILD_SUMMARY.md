# PulseCodeAI Build & Launch Summary

## Goal
Get the PulseCodeAI IDE (a VS Code OSS fork) to launch successfully via `scripts/code.bat` without crashes or missing native C++ binary exceptions.

## What Was Done

### 1. Corrected Launch Script & Workspace Issues
- Fixed the typo in `desktop/scripts/code.bat` where it used `NAMESHIRT` instead of `NAMESHORT`.
- Refactored `findstr` in the batch file to query `"nameShort"` explicitly (avoiding false matches like `win32ShellNameShort`).
- Quoted the `%CODE%` path executions to ensure directories containing spaces are supported.
- Created the missing `.github/copilot-instructions.md` file required by the postinstall script.

### 2. Configured C++ Toolchain Support
- Updated global `npm` to ensure `node-gyp@13.0.0` was present to properly interface with the Visual Studio 2026/2022 C++ compiler toolchain.
- Removed arbitrary overrides (like `msvs_version=2022`) from `.npmrc` to allow the build environment to auto-discover the local Visual Studio installation.

### 3. Rebuilt Native C++ Modules for Electron 42.6.0
Native modules compiled for standard Node.js threw architecture/ABI mismatch errors when run inside Electron. To resolve this, `@electron/rebuild` was installed and run to build native `.node` modules specifically compiled for the Electron 42.6.0 ABI (ABI 146).
This successfully compiled and linked the C++ binaries for:
- `@vscode/policy-watcher`
- `@vscode/sqlite3`
- `windows-foreground-love`
- `@vscode/windows-process-tree`
- `@vscode/windows-registry`
- `@parcel/watcher`
- `node-pty`
- `@vscode/spdlog`

---

## Commands to Launch the IDE

From the `desktop` workspace root (`D:\vscode_src\desktop`):

```powershell
.\scripts\code.bat
```

No developer environment variables or terminal exports are needed. The IDE will start up immediately with the built-in Pulse Agent extension fully integrated and active.

---
*Updated on 2026-07-16*