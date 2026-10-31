# Space Haven《太空避难所》存档修改器（单文件版）
# Space Haven Save Editor — Single-File Edition

可视化修改 Space Haven 存档的单文件 Windows 工具，双击即用，无需安装 Python、无需浏览器。
A single-file Windows tool to visually edit Space Haven saves. Double-click to run — no Python install, no browser needed.

> 游戏 **Space Haven** 中文名：《太空避难所》/《太空避风港》（简体）。
> The game **Space Haven** is also known in Chinese as 《太空避难所》/《太空避风港》(Simplified).

## 下载 / Download
从 Releases 下载 `SpaceHavenSaveEditor.exe`。
Download `SpaceHavenSaveEditor.exe` from Releases.

## 兼容版本 / Compatibility
本工具基于 **Space Haven 1.0.0（Steam build 5）** 的存档结构制作，适用于 **1.0.0 及以上**版本。
Built from the **Space Haven 1.0.0 (Steam build 5)** save format; compatible with **1.0.0 and above**.
> 若游戏更新后存档结构发生重大变化，内部定义可能失效，编辑前请确认与你的游戏版本一致。
> If a game update significantly changes the save format, the built-in definitions may become outdated — verify they match your game version before editing.

## 功能 / Features
- 单文件 exe，原生窗口运行，不依赖浏览器
  Single-file exe running in a native window — no browser required
- 中英双语界面，右上角一键切换
  Bilingual (Chinese / English) UI, one-click switch at the top-right
- 船员属性 / 技能 / 特质编辑，每个船员一键全满
  Edit crew attributes / skills / traits; one-click "fill all" per crew member
- 物品增删、飞船资金修改、原始 XML 直接编辑
  Add/remove items, edit ship credits, edit the raw XML directly
- 自动备份、冲突检测、安全写回（仅偏移定点替换）
  Automatic backups, conflict detection, safe write-back (offset-fixed patching only)
- 存档目录由用户手动指定，不自动探测任何位置
  Save directory is set manually by the user — no automatic detection of any location

## 使用 / Usage
1. 双击 `SpaceHavenSaveEditor.exe`。
   Double-click `SpaceHavenSaveEditor.exe`.
2. 首次使用请点「存档目录」按钮，手动指定 `savegames` 目录本身（工具不会自动探测）。
   On first use, click "存档目录 / Save Directory" and manually point it at the `savegames` folder itself (the tool does not auto-detect).
3. 编辑后点「保存到存档」。若游戏正在运行，请先在游戏内重新载入以确认。
   After editing, click "保存到存档 / Save to Save". If the game is running, reload in-game first to confirm.

> 写入前会自动备份到 `C:\Users\<你>\AppData\Local\SpaceHavenSaveEditor\backups`。
> Backups are written automatically before saving, to `C:\Users\<you>\AppData\Local\SpaceHavenSaveEditor\backups`.
> 若存档被外部修改（例如游戏刚自动保存过），工具会提示冲突，需先「重新载入」再重做更改。
> If the save was modified externally (e.g. the game just auto-saved), the tool reports a conflict — reload first, then re-apply your changes.

## 从源码构建 / Build from source
需要 Windows + Python 3.13：
Requires Windows + Python 3.13:
```bat
pip install pyinstaller pywebview
build_exe.bat
```
产物位于 `dist\SpaceHavenSaveEditor.exe`。
Output: `dist\SpaceHavenSaveEditor.exe`.

## 安全说明 / Safety
- 工具只做偏移定点替换，保证存档其余字节与游戏原始输出完全一致。
  The tool only performs offset-fixed patching, keeping every other byte identical to the game's original output.
- 修改存档存在风险，请自行备份重要进度。
  Editing saves carries risk — back up important progress yourself.
- 本工具仅供学习研究，使用后果自负。
  For learning and research only; use at your own risk.
