# 织梦工作台前端

这是织梦通用 Agent 的 React + TypeScript 工作台。第一屏是对话和项目操作，小说只是可按需挂载的 Skills 场景。当前界面以桌面使用为主，窄屏布局保留但不是本轮主要验收目标。

## 当前界面

- 左栏管理自由对话和文件夹项目；项目可以新建或打开已有文件夹。
- 中栏显示当前对话、模型、权限、附件、工具过程、失败恢复和输入区。
- 右栏显示文件、修改记录和持久命令会话。
- 设置页包含模型服务、MCP 工具、聊天偏好、外观、记忆、技能库、项目说明和运行状态。
- 模型设置同时展示模型当前有效的 Chat、Responses 或 Messages 接口；错误提示保留真实 HTTP/网络原因，可收起或关闭。
- 输入区提供回车发送、Shift+Enter 换行。应用级键盘快捷键不属于产品交互。

常用文件入口是 `src/app/Root.tsx`、`src/app/chat/ChatView.tsx`、`src/app/chat/FailureNotice.tsx`、`src/app/settings/ModelsSection.tsx` 和 `src/api`。布局、颜色和信息层级约定见 `src/app/DESIGN.md`。

## 启动

```powershell
cd C:\Users\demo\Desktop\新项目\apps\workbench
npm install
npm run dev
```

Vite 默认从 `http://127.0.0.1:5173` 开始选择端口。前端默认请求同源的 Writing Runtime；正式作者服务由仓库根目录的启动脚本管理，隔离浏览器走查使用 5175/8788。

## 验证

```powershell
npm run test
npm run typecheck
npm run build
npm run lint
```

涉及对话、文件、命令、MCP 或模型设置的改动，要在定点测试之外使用隔离 Runtime 和项目副本做真实任务。单元测试通过只说明代码路径通过，不能代替模型线路和完整桌面流程验收。

## 前端边界

前端只呈现后端真实的运行、工具、文件和错误状态，不把模型的文字承诺当作文件已修改，也不把 MCP 外部写入冒充织梦的逐文件撤回。小说质量、Skills 内容和记忆提取质量分别在后端/Skills 专项验收；不要用小说章节题代替通用 Agent 验收。
