# 织梦本地 Agent 工作台

织梦是本地通用 Agent 工作台。项目是文件夹，Agent 可以聊天、读取和修改文件、处理文档表格、执行命令、使用 MCP 和按需 Skills。小说等专业流程通过 Skills 提供。

左栏管理对话和项目，中间显示任务、回答和工具过程，右栏查看文件、修改记录、终端与共享浏览器。文件工具保存时记录版本，可查看差异并撤回；命令只为明确指定的观察路径保留文件变化记录。

当前提供 Windows 源码开发版。完整子 Agent 和安装包发行尚未完成。

## 从源码启动

需要 Windows 10/11、PowerShell 7、Python 3.12、Node.js 22.12 或更新版本。

```powershell
git clone https://github.com/le5444/zhimeng-longform-studio.git
cd zhimeng-longform-studio
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\services\writing-runtime[dev]"
npm ci
& ./scripts/start_author_workbench.ps1 -NoBrowser -SkipBookshelfScan
```

打开 `http://127.0.0.1:5173`，在设置里填写自己的模型连接，然后开始对话或打开项目文件夹。运行时监听本机 8787 端口。使用 `scripts/stop_author_workbench.ps1` 停止服务。

桌面共享浏览器使用独立 Electron 宿主：

```powershell
npm --prefix apps/desktop ci
& ./scripts/start_desktop_workbench.ps1
```

真实凭据、会话数据库和用户文件留在本机，不包含在源码中。Office 预览提取文字及表格信息，不是完整排版编辑器。模型别名可调用不代表已验证其模型身份。

## 结构与检查

- `apps/workbench`：React / TypeScript 界面。
- `apps/desktop`：Electron 桌面与共享浏览器。
- `services/writing-runtime`：Agent、协议、SQLite、文件和命令执行。
- `packages/contracts`：前后端合同。
- `adapters/builtin`：按需领域适配器。

```powershell
npm run typecheck
npm run test:workbench
npm run build
$env:PYTHONPATH = 'services/writing-runtime/src'
.\.venv\Scripts\python.exe -m pytest services/writing-runtime/tests -n 6
```

真实文件写入验收使用独立数据库与临时文件。开发版本同步范围见 [源码快照说明](docs/public-snapshot.md)，本轮修复见 [文件工作流](docs/agent-file-workflows.md)。

项目体积、当前重构边界和开发快照入口见 [项目减负记录](docs/project-slimdown.md)。需要开发检查点时使用 `python scripts/dev_snapshot.py capture .runtime/workbench.sqlite3 <label>`；按内容压缩去重，避免每个任务目录各存一份完整数据库。
