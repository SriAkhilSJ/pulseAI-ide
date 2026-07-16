# Quick Start: Building and Running VS Code from Source

## Prerequisites
- Windows 10
- Node.js v24.18.0 (as per .nvmrc)
- pnpm (comes with Node.js via corepack, or install separately)
- Visual Studio 2022 Community (or Professional/Enterprise) with "Desktop development with C++" workload

## Steps

### 1. Clone the Repository (if not already done)
```bash
git clone https://github.com/microsoft/vscode.git
cd vscode
```

### 2. Install Dependencies
Use pnpm to install dependencies (this is faster and more reliable than npm/yarn for this repo):
```bash
pnpm install
```

### 3. Build the TypeScript Source
Set the environment variable to skip postinstall scripts (which can cause issues with native modules on Windows) and then compile:
```bash
VSCODE_SKIP_POSTINSTALL=1 pnpm compile
```
This step may take a few minutes. Look for exit code 0 to confirm success.

### 4. Run VS Code
After the build completes, launch the editor using the provided script:
```bash
scripts\code.bat
```
You should see the VS Code window open and begin synchronizing built-in extensions.

## Troubleshooting

### Native Module Build Issues
If you encounter errors building native modules (like `@vscode/policy-watcher`), note that:
- The core VS Code editor can start and function without these modules being built (they are loaded lazily or optional).
- If you need to modify native modules, ensure:
  - Visual Studio 2022 is installed with the C++ workload.
  - You open a **Developer Command Prompt for VS 2022** (via Start Menu) and run the build commands from there.
  - Alternatively, use the following in a regular command prompt:
    ```bash
    call "C:\Program Files\Microsoft Visual Studio\18\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64
    cd /d /path/to/vscode
    pnpm rebuild <module-name> --build-from-source
    ```

### Common Errors and Fixes
- **"msvs_version not set"**: Set `GYP_MSVS_VERSION=2022` in your environment before running `npm rebuild`.
- **Path quoting issues in batch files**: Avoid nested quotes. Use short paths (8.3 notation) or set variables without extra quotes.
- **Node.js version mismatch**: Ensure you are using Node v24.18.0. Use `nvm use 24.18.0` if using nvm-windows.

## Notes
- The `VSCODE_SKIP_POSTINSTALL=1` flag skips scripts that attempt to rebuild native modules during `pnpm install`, which can fail in complex environments. The TypeScript build (`pnpm compile`) is the critical step for most development work.
- If you plan to work on extensions or core features that do not require modifying native components, you likely do not need to build the native modules at all.

## References
- Official VS Code Contribution Guide: https://github.com/microsoft/vscode/blob/main/docs/gettingstarted.md
- Building VS Code on Windows: https://github.com/microsoft/vscode/wiki/How-to-Contribute#build