@echo off
rem Get the Visual Studio installation path using vswhere
for /f "usebackq delims=" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.Component.MSBuild -property productPath`) do set "VSINSTALLDIR=%%~dpi"
rem Remove the trailing backslash
set "VSINSTALLDIR=%VSINSTALLDIR:~0,-1%"
set "VCINSTALLDIR=%VSINSTALLDIR%\VC"
set "GYP_MSVS_VERSION=2022"
call "%VSINSTALLDIR%\VC\Auxiliary\Build\vcvarsall.bat" x64
cd /d D:\vscode_src\desktop\node_modules\.pnpm\@vscode+policy-watcher@1.4.0\node_modules\@vscode\policy-watcher
npm rebuild