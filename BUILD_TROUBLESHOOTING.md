# VS Code Build Troubleshooting & Launch Resolution Summary

## Objective
Get `scripts/code.bat` to run successfully to launch VS Code after building the repository, resolving all runtime native module load errors.

## Environment
- OS: Windows 10
- Node.js: v24.18.0 (from .nvmrc)
- Package Manager: pnpm
- Visual Studio: Community 2026/2022 (v18.8.12009.203) at `C:\Program Files\Microsoft Visual Studio\18\Community`
- Electron Target: 42.6.0 (ABI 146)

---

## 1. What Actually Prevented Launch (The Real Issues)
While the TypeScript compiler successfully generated JavaScript output in `out/`, launching `scripts/code.bat` failed due to the following critical issues:

1. **Typo in `code.bat`**: A typo (`NAMESHIRT` instead of `NAMESHORT` in variables) prevented extracting the correct branding configuration. Additionally, `findstr` matched duplicate keys (`win32ShellNameShort`), and paths containing spaces were not quoted.
2. **Missing `postinstall` Files**: The installation crashed because `.github/copilot-instructions.md` was missing, breaking symlink creation during the install script.
3. **Electron vs Node ABI Version Mismatch**: VS Code OSS depends on native C++ modules. Standard compilation produced binaries targeting Node.js v24.x. When loaded inside Electron v42.6.0 (which has ABI 146), Electron threw `MODULE_NOT_FOUND` and architecture mismatch exceptions at startup.
4. **Visual Studio 2026/2022 Detection Failures**: The previous tool's `node-gyp@10.2.0` configuration was unable to recognize the newer Visual Studio Community v18 C++ toolchain without throwing `unknown version` configuration errors.

---

## 2. The Resolutions That Worked

### Fixing the Launch Script & Workspace Setup
- Fixed the typos in `desktop/scripts/code.bat`, corrected the exact key query in `findstr`, and enclosed the execution command in quotes (`"%CODE%"`) to correctly handle spaces in directory paths.
- Created the missing `.github/copilot-instructions.md` file to satisfy the `postinstall` symlink tasks.

### Fixing Visual Studio Detection
- Updated global `npm` to obtain `node-gyp@13.0.0`, which natively supports Visual Studio 2026/2022.
- Cleaned up manual overrides (like `msvs_version=2022`) from `.npmrc` to allow the build environment to auto-discover the compiler toolchain.

### Rebuilding Native Modules for Electron 42.6.0
- Installed `@electron/rebuild` in the `desktop` workspace.
- Rebuilt all native C++ binaries specifically targeting the Electron 42.6.0 ABI. This compiled and generated correct `.node` binaries in `bin/win32-x64-146/` and `build/Release/` folders for:
  - `@vscode/policy-watcher`
  - `@vscode/sqlite3`
  - `windows-foreground-love`
  - `@vscode/windows-process-tree`
  - `@vscode/windows-registry`
  - `@parcel/watcher`
  - `node-pty`
  - `@vscode/spdlog`

---

## 3. How to Launch the IDE

To run the fully rebuilt and customized IDE:
```powershell
cd d:\vscode_src\desktop
.\scripts\code.bat
```

No developer environment variables or terminal exports are needed. The IDE will start up immediately with integrated chat, agent, and workspace modules fully loaded.