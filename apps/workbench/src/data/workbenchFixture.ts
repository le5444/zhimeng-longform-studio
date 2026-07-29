export type ChapterState = 'done' | 'draft' | 'review' | 'blocked'

export interface ChapterItem {
  id: string
  number: string
  title: string
  words: number
  state: ChapterState
}

export interface VolumeItem {
  id: string
  title: string
  progress: string
  chapters: ChapterItem[]
}

export const volumes: VolumeItem[] = [
  {
    id: 'v1',
    title: '第一卷 · 雾城来客',
    progress: '12 / 18',
    chapters: [
      { id: 'c10', number: '010', title: '旧港口', words: 3184, state: 'done' },
      { id: 'c11', number: '011', title: '灯下的名字', words: 2941, state: 'done' },
      { id: 'c12', number: '012', title: '雨夜来信', words: 2678, state: 'review' },
      { id: 'c13', number: '013', title: '借来的钥匙', words: 1832, state: 'draft' },
      { id: 'c14', number: '014', title: '沉默证人', words: 0, state: 'blocked' },
    ],
  },
  {
    id: 'v2',
    title: '第二卷 · 潮痕',
    progress: '0 / 16',
    chapters: [
      { id: 'c19', number: '019', title: '返潮', words: 0, state: 'draft' },
      { id: 'c20', number: '020', title: '空房间', words: 0, state: 'draft' },
    ],
  },
]

export const currentDraft = `
  <h1>第十二章 雨夜来信</h1>
  <p>雨是从傍晚开始下的。</p>
  <p>先是一层薄雾贴着旧港口的石阶，等林见川从档案馆出来，檐角的水已经连成了线。他把那封没有署名的信塞进大衣内袋，沿着河堤往南走。</p>
  <p>信纸很薄，隔着布料仍能摸到一道折痕。上面只有一句话：<strong>别让周鹤亭看见钥匙。</strong></p>
  <p>林见川在第三盏路灯下停住。河对岸的仓库没有亮灯，铁门前却多了一辆黑色轿车。车灯熄着，雨刷每隔几秒摆动一次，像有人在黑暗里眨眼。</p>
  <blockquote>他忽然想起父亲失踪前的那个晚上。也是这样的雨，也是这样一辆没有开灯的车。</blockquote>
  <p>身后传来鞋跟踩过积水的声音。</p>
  <p>“你比约定晚了七分钟。”女人说。</p>
  <p>林见川没有回头。他把手伸进口袋，指尖碰到那把黄铜钥匙。齿纹上还沾着档案柜里的灰。</p>
  <p>“我没有跟任何人约过。”</p>
  <p>“那封信不是约你。”女人走到他身侧，伞沿压得很低，“是提醒我，在你做出错误决定之前拦住你。”</p>
  <p>河面传来一声短促的汽笛。黑色轿车的雨刷停了。</p>
  <p>女人抬起脸。林见川终于看清她右眼下方那颗很淡的痣，也认出了她。</p>
  <p>三年前，在父亲葬礼上，她坐在最后一排。</p>
`

export const candidateExcerpt = [
  '雨声压低了整座旧港口。林见川把信折回原样，沿河堤向南。',
  '第三盏路灯下，他看见了那辆不该出现的黑色轿车。',
  '父亲失踪那晚被压在记忆深处的画面，第一次有了清楚的边缘。',
]

export const contextSources = [
  { name: '本章控制卡', detail: '使命、必须事件、禁改项', tokens: '1.2k' },
  { name: '人物状态', detail: '林见川 / 周鹤亭 / 苏棠', tokens: '860' },
  { name: '最近正文', detail: '第 010-011 章 + 本章窗口', tokens: '4.8k' },
  { name: '未偿伏笔', detail: '黄铜钥匙、黑色轿车、葬礼来客', tokens: '940' },
]

export const gateItems = [
  { level: 'P0', state: 'pass', title: '时间线与人物关系', detail: '未发现硬冲突' },
  { level: 'P1', state: 'warn', title: '本章必须事件', detail: '周鹤亭的压力尚未落到场内' },
  { level: 'P1', state: 'pass', title: '章间接缝', detail: '承接第 011 章档案馆线索' },
  { level: 'P2', state: 'warn', title: '叙事节奏', detail: '中段连续解释可压缩约 120 字' },
]

export const timelineEvents = [
  { time: '11:42:18', type: 'context', label: '装配本章上下文', detail: '8 sources · 8.1k tokens' },
  { time: '11:42:21', type: 'skill', label: '注入 chapter-brief', detail: 'instruction skill' },
  { time: '11:42:25', type: 'gate', label: 'Prewrite gate', detail: '2 pass · 1 warning' },
  { time: '11:42:31', type: 'agent', label: 'Writer 等待 Runtime', detail: '未开始模型调用' },
]
