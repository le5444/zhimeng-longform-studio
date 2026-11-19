import type { ServiceForm } from './modelForm'

/** Editable connection defaults; model availability comes from the user's service. */
export const PROVIDER_PRESETS: Array<{ name: string; values: Partial<ServiceForm> }> = [
  { name: 'DeepSeek', values: { name: 'DeepSeek', baseUrl: 'https://api.deepseek.com', wireFormat: 'chat', authType: 'bearer' } },
  { name: 'OpenAI', values: { name: 'OpenAI', baseUrl: 'https://api.openai.com/v1', wireFormat: 'responses', authType: 'bearer' } },
  { name: 'Anthropic', values: { name: 'Anthropic', baseUrl: 'https://api.anthropic.com', wireFormat: 'anthropic_messages', authType: 'x-api-key' } },
  { name: 'Ollama', values: { name: 'Ollama', baseUrl: 'http://localhost:11434/v1', wireFormat: 'chat', authType: 'none' } },
  { name: '自定义', values: {} },
]
