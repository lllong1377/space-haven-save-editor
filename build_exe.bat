@echo off
REM 构建单文件 exe（原生窗口，无服务）。需先安装依赖：
REM   pip install pyinstaller pywebview
REM 产物在 dist\SpaceHavenSaveEditor.exe
setlocal
cd /d "%~dp0"
python -m PyInstaller --onefile --windowed ^
  --name SpaceHavenSaveEditor ^
  --add-data "index.html;." ^
  --add-data "defs.json;." ^
  --collect-all webview ^
  --hidden-import webview ^
  save_editor.py
echo.
echo 完成。产物位于 dist\SpaceHavenSaveEditor.exe
endlocal
