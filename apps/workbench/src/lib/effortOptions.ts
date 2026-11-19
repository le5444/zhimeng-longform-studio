/** 思考参数的请求偏好。家族只帮助排列选项，不证明渠道支持某档。 */
import type { ProviderReasoningEffort } from '../api/providerSettings'

export interface EffortOption {
  value: ProviderReasoningEffort
  label: string
  hint: string
}

export interface EffortFamily {
  label: string
  note: string
  efforts: EffortOption[]
}

const AUTO: EffortOption = { value: 'auto', label: '自动', hint: '不发送档位参数，使用服务默认值' }
const NONE: EffortOption = { value: 'none', label: '关闭', hint: '请求关闭思考，是否支持取决于服务' }
const LOW: EffortOption = { value: 'low', label: '低', hint: '简单问答和小改' }
const MEDIUM: EffortOption = { value: 'medium', label: '中', hint: '日常构思与审读' }
const HIGH: EffortOption = { value: 'high', label: '高', hint: '复杂剧情和设定核对' }
const XHIGH: EffortOption = { value: 'xhigh', label: '极高', hint: '请求 xhigh；是否支持取决于型号与服务' }
const MAX: EffortOption = { value: 'max', label: '最大', hint: '请求 max；名称不保证达到模型最高强度' }
const ULTRA: EffortOption = { value: 'ultra', label: '超高', hint: '请求 ultra；是否支持取决于型号与服务' }

/**
 * 按模型名匹配家族；找不到走「通用」全档位。
 * 选项是请求偏好，不是经过验证的能力表。
 * 扩展档位遭明确拒收时先尝试 high，再移除参数使用服务默认值。
 */
export const EFFORT_FAMILIES: Array<{ pattern: RegExp } & EffortFamily> = [
  {
    pattern: /gpt|^o[134](-|$)|openai/i,
    label: 'GPT / OpenAI 系',
    note: '这些是请求档位；具体型号和服务不一定支持极高。拒收后会兼容重试。',
    efforts: [AUTO, LOW, MEDIUM, HIGH, XHIGH],
  },
  {
    pattern: /glm|zhipu|chatglm/i,
    label: 'GLM / 智谱系',
    note: '最大表示请求 max，不保证服务支持或采用该档位；拒收后会兼容重试。',
    efforts: [AUTO, NONE, LOW, MEDIUM, HIGH, MAX],
  },
  {
    pattern: /deepseek/i,
    label: 'DeepSeek 系',
    note: '这些是请求档位；是否支持取决于型号与服务，不能由家族名称确认。',
    efforts: [AUTO, NONE, LOW, MEDIUM, HIGH, MAX],
  },
  {
    pattern: /qwen|kimi|moonshot|minimax|baichuan|yi-/i,
    label: '国产大模型系',
    note: '不同型号支持的参数不同；最大是请求偏好，不是能力保证。',
    efforts: [AUTO, NONE, LOW, MEDIUM, HIGH, MAX],
  },
  {
    pattern: /grok/i,
    label: 'Grok 系',
    note: '这些是请求档位；是否支持取决于型号与服务，不能由家族名称确认。',
    efforts: [AUTO, LOW, MEDIUM, HIGH, MAX],
  },
]

const ALL_EFFORTS: EffortOption[] = [AUTO, NONE, LOW, MEDIUM, HIGH, XHIGH, MAX, ULTRA]

export function resolveEffortFamily(model: string | null | undefined): EffortFamily {
  const name = (model ?? '').trim()
  if (name) {
    for (const family of EFFORT_FAMILIES) {
      if (family.pattern.test(name)) return family
    }
  }
  return {
    label: '通用',
    note: '档位支持情况未验证。扩展档被拒后先试 high，再自动去掉参数按服务默认值重试。',
    efforts: ALL_EFFORTS,
  }
}

export function effortOptionLabel(value: ProviderReasoningEffort): string {
  return ALL_EFFORTS.find((item) => item.value === value)?.label
    ?? ({ xhigh: '极高', max: '最大', ultra: '超高' } as Record<string, string>)[value]
    ?? '自动'
}
