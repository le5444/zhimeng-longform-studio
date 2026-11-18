// 输入框的 @ 文件清单和 / 命令清单（9-29 重做：不按正文 / 人物分类，/ 里只有真命令和技能）。
import { describe, expect, it } from 'vitest'
import { COMMANDS, commandOptions, commandQuery, mentionAt, mentionedFileIds, mentionOptions } from '../chat/composerLogic'
import { makeFile } from './fixtures'
import { formatFileMention } from '../lib/fileReferences'

const FILES = [
  makeFile('README.md'),
  makeFile('src/app/Root.tsx'),
  makeFile('docs/设计说明.md'),
  makeFile('05_正文/第001章_山门.md'),
  makeFile('notes/readme-old.txt'),
]

describe('@ 引用文件', () => {
  it('单价不自动附成数字文件，选择数字文件时生成明确的带引号引用', () => {
    const files = [makeFile('19.50'), makeFile('12'), makeFile('2026.csv')]
    expect(mentionedFileIds('2 件 @19.50、3 件 @12，见 @2026.csv', files)).toEqual(['file:2026.csv'])
    expect(formatFileMention('19.50')).toBe('@"19.50"')
    expect(mentionedFileIds(formatFileMention('19.50'), files)).toEqual(['file:19.50'])
    expect(mentionedFileIds('@./19.50', files)).toEqual(['file:19.50'])
  })

  it('代码和已有链接中的 @ 保留原文，只附上普通 Markdown 文本明确引用的文件', () => {
    const file = makeFile('private.txt')
    const text = [
      '`@private.txt`',
      '```text\n@private.txt\n```',
      '~~~text\n@private.txt\n~~~',
      '> ```text\n> @private.txt\n> ```',
      '    @private.txt',
      '[@private.txt](https://example.com)',
      '[@private.txt][reference]',
      '[reference]: https://example.com',
    ].join('\n\n')
    expect(mentionedFileIds(text, [file])).toEqual([])
    expect(mentionedFileIds(text + '\n\n请阅读 **@private.txt**', [file])).toEqual([file.file_id])
  })

  it('含空格/括号的完整引用和代码行号会附上准确文件，邮箱不算附件', () => {
    const files = [makeFile('data/sales (2026).csv'), makeFile('src/main.py'), makeFile('README')]
    expect(mentionedFileIds('检查 @"data/sales (2026).csv"、@src/main.py:12 与 @README', files)).toEqual(files.map((file) => file.file_id))
    expect(mentionedFileIds('mail@README', files)).toEqual([])
    expect(mentionedFileIds('看看@src/main.py', files)).toEqual(['file:src/main.py'])
    expect(mentionedFileIds('https://example.com/@README', files)).toEqual([])
    expect(mentionAt('读取 @"sales 20')).toMatchObject({ query: 'sales 20' })
    expect(mentionAt('读取 @"sales 2026.csv"')).toBeNull()
  })

  it('两个目录同名文件不会误附两份，选择完整路径只附所选文件', () => {
    const files = [makeFile('src/main.py'), makeFile('tests/main.py')]
    expect(mentionedFileIds('@main.py', files)).toEqual([])
    expect(mentionedFileIds('@src/main.py', files)).toEqual(['file:src/main.py'])
  })

  it('光标前面是 @ 加几个字就在引用；邮箱、空格之后不算', () => {
    expect(mentionAt('看看 @Ro')).toEqual({ query: 'Ro', start: 3, end: 6 })
    expect(mentionAt('@')).toEqual({ query: '', start: 0, end: 1 })
    expect(mentionAt('发到 me@example.com')).toBeNull()
    expect(mentionAt('@Root 这个文件')).toBeNull()
    expect(mentionAt('没有艾特')).toBeNull()
    // 光标停在中间：只看光标前面
    expect(mentionAt('@Ro 然后', 3)).toEqual({ query: 'Ro', start: 0, end: 3 })
  })

  it('名字开头对上的排最前，再是名字里有、路径里有；没打字就按路径短的排', () => {
    expect(mentionOptions(FILES, 'read').map((item) => item.path)).toEqual(['README.md', 'notes/readme-old.txt'])
    expect(mentionOptions(FILES, 'app').map((item) => item.path)).toEqual(['src/app/Root.tsx'])
    expect(mentionOptions(FILES, '山门')[0]).toMatchObject({ name: '第001章_山门.md', folder: '05_正文' })
    expect(mentionOptions(FILES, '').map((item) => item.path)[0]).toBe('README.md')
    expect(mentionOptions(FILES, '', 2)).toHaveLength(2)
    expect(mentionOptions(FILES, '没有这个')).toEqual([])
  })

  it('发出去的话里 @ 了哪些文件：认路径、文件名、不带扩展名的文件名', () => {
    expect(mentionedFileIds('看一下 @README.md 和 @src/app/Root.tsx', FILES)).toEqual(['file:README.md', 'file:src/app/Root.tsx'])
    expect(mentionedFileIds('@设计说明，改一下', FILES)).toEqual(['file:docs/设计说明.md'])
    expect(mentionedFileIds('没有引用', FILES)).toEqual([])
    expect(mentionedFileIds('@不存在.md', FILES)).toEqual([])
  })
})

describe('/ 命令和技能', () => {
  it('整段以 / 开头、还没换行才算在打命令', () => {
    expect(commandQuery('/压缩')).toBe('压缩')
    expect(commandQuery('/')).toBe('')
    expect(commandQuery('帮我 /压缩')).toBeNull()
    expect(commandQuery('/压缩\n第二行')).toBeNull()
  })

  it('只有真命令和技能，没有写好的话；没打字时技能只露前 6 个', () => {
    const all = commandOptions('', ['a', 'b', 'c', 'd', 'e', 'f', 'g'])
    expect(all.filter((item) => item.kind === 'command')).toHaveLength(COMMANDS.length)
    expect(all.filter((item) => item.kind === 'skill').map((item) => item.label)).toEqual(['a', 'b', 'c', 'd', 'e', 'f'])
    expect(COMMANDS.every((item) => item.action)).toBe(true)
  })

  it('按名字、说明、拼音 / 英文别名找', () => {
    expect(commandOptions('压缩', []).map((item) => item.key)).toEqual(['compact'])
    expect(commandOptions('yasuo', []).map((item) => item.key)).toEqual(['compact'])
    expect(commandOptions('undo', []).map((item) => item.key)).toEqual(['rollback-last'])
    expect(commandOptions('小说', ['小说创作', '番茄下书蒸馏']).map((item) => item.key)).toEqual(['skill:小说创作'])
  })
})
