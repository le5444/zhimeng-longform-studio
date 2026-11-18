import { BookOpen, Brain, Cpu, FileText, Info, Plug, SlidersHorizontal } from 'lucide-react'
import { S } from './settingsText'

export const SETTINGS_PAGES = [
  { key: 'general', label: '通用', icon: SlidersHorizontal, group: '偏好', scope: '当前浏览器', description: '外观和聊天习惯。修改后自动保存到当前浏览器。' },
  { key: 'models', label: S.nav.models, icon: Cpu, group: '连接与能力', scope: '本机模型服务', description: '填写接口地址、密钥和模型，保存后即可选择使用。' },
  { key: 'mcp', label: 'MCP 工具', icon: Plug, group: '连接与能力', scope: '本地和网络服务', description: '连接本地程序或网络服务，扩展 Agent 可调用的工具。' },
  { key: 'skills', label: S.nav.skills, icon: BookOpen, group: '连接与能力', scope: '本机技能库', description: '管理按需加载的专业能力，查看来源和使用说明。' },
  { key: 'memory', label: S.nav.memory, icon: Brain, group: '上下文', scope: '本机用户记忆', description: '查看和编辑长期记忆，保留每次修改前的版本。' },
  { key: 'project', label: '项目说明', icon: FileText, group: '上下文', scope: '当前项目', description: '为当前文件夹提供工作约定和持续上下文。' },
  { key: 'about', label: S.nav.about, icon: Info, group: '应用', scope: '本机运行状态', description: '检查本机运行状态，获取排查问题需要的信息。' },
] as const

export type SettingsPage = typeof SETTINGS_PAGES[number]['key']
/** Older links and saved preferences continue to open their controls. */
export type SettingsSection = SettingsPage | 'appearance' | 'behavior'
export function settingsPage(section: SettingsSection): SettingsPage {
  return section === 'appearance' || section === 'behavior' ? 'general' : section
}

export const SETTING_ITEMS: Array<{ section: SettingsSection; id: string; label: string; keywords: string }> = [
  { section: 'mcp', id: 'mcp-servers', label: 'MCP 工具连接', keywords: 'MCP stdio HTTP URL 工具 服务 程序 参数 环境变量 地址 请求头 headers 连接 启用' },
  { section: 'models', id: 'connection', label: '连接与当前模型', keywords: 'URL Key API 地址 密钥 模型 默认 连接' },
  { section: 'models', id: 'model-list', label: '可用模型', keywords: '获取 拉取 搜索 默认 勾选 手动添加' },
  { section: 'models', id: 'model-tests', label: '线路与连接测试', keywords: '测活 测试 自定义内容 协议 验证 记住' },
  { section: 'models', id: 'model-options', label: '思考强度与历史容量（此服务共用）', keywords: 'token 长度 窗口 256K 1M' },
  { section: 'models', id: 'model-advanced', label: '接口与高级连接选项', keywords: '名称 Chat Responses Anthropic 自动 协议 认证 请求头 headers UA' },
  { section: 'general', id: 'thinking', label: S.behavior.thinking, keywords: '聊天 偏好 思考 展开' },
  { section: 'general', id: 'group-tools', label: S.behavior.groupTools, keywords: '聊天 偏好 工具 合并 分组' },
  { section: 'general', id: 'save-policy', label: '修改建议的保存方式', keywords: '保存 自动 审阅 文件 修改' },
  { section: 'general', id: 'theme', label: '主题', keywords: '外观 亮色 暗色 跟随系统' },
  { section: 'general', id: 'ui-size', label: S.uiSize, keywords: '外观 字体 字号 大小 显示' },
  { section: 'memory', id: 'memory-content', label: '用户记忆', keywords: '记忆 搜索 编辑 删除 添加 开关' },
  { section: 'memory', id: 'memory-raw', label: '记忆全文编辑', keywords: '记忆 全文 文件 草稿 修改' },
  { section: 'memory', id: 'memory-history', label: '记忆修改历史', keywords: '历史 版本 恢复' },
  { section: 'skills', id: 'skill-list', label: '技能目录', keywords: 'Skills 技能 搜索 筛选 编辑 新建' },
  { section: 'skills', id: 'skill-sources', label: '技能来源', keywords: '共享 文件夹 Codex Claude 来源' },
  { section: 'project', id: 'project-instructions', label: '当前项目说明文件', keywords: '项目 AGENTS CLAUDE 规则 文件夹' },
  { section: 'about', id: 'runtime', label: '本机服务状态', keywords: '关于 版本 连接 检查 诊断' },
]

export function searchSettings(query: string) {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  return SETTING_ITEMS.filter((item) => terms.every((term) => `${item.label} ${item.keywords}`.toLocaleLowerCase().includes(term)))
}
