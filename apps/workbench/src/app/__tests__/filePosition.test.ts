import { describe, expect, it } from 'vitest'
import { fileTextSelection } from '../lib/filePosition'

describe('fileTextSelection', () => {
  it('CRLF、CR和空行按原始字符偏移定位，范围末列包含在选区中', () => {
    const text = 'first\r\n\r\nthird\rfourth\n'
    const range = fileTextSelection(text, { line: 3, column: 2, endLine: 4, endColumn: 3 })!
    expect(text.slice(range.start, range.end)).toBe('hird\rfou')
    expect(fileTextSelection(text, { line: 2 })).toEqual({ start: 7, end: 7 })
  })

  it('缺失起点不猜测；尾部范围超过文件时只选实际剩余内容', () => {
    expect(fileTextSelection('', { line: 1 })).toBeNull()
    expect(fileTextSelection('one\n', { line: 2 })).toBeNull()
    expect(fileTextSelection('one\n', { line: 1, column: 9 })).toBeNull()
    expect(fileTextSelection('one\ntwo', { line: 2, endLine: 8 })).toEqual({ start: 4, end: 7 })
  })
})
