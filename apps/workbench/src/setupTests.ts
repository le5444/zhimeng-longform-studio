import '@testing-library/jest-dom/vitest'
import { webcrypto } from 'node:crypto'

// jsdom 不提供浏览器安全上下文里的 SubtleCrypto；用 Node 的同一 WebCrypto 接口。
if (!globalThis.crypto?.subtle) Object.defineProperty(globalThis, 'crypto', { configurable: true, value: webcrypto })

// prosemirror-view 滚动到选区时会调 Range / Element 的 getClientRects 与 getBoundingClientRect，
// jsdom 没实现，整套 vitest 会以「Errors 1 error」退出码 1 收场（用例本身全过，9-21/22 三轮都见过）。
// 这是测试环境噪音不是产品 bug：给个空实现，让全绿就是全绿。
const emptyRect = (): DOMRect =>
  ({ x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON: () => ({}) }) as DOMRect
const emptyRectList = (): DOMRectList =>
  ({ length: 0, item: () => null, [Symbol.iterator]: function* () {} }) as unknown as DOMRectList
for (const proto of [typeof Range !== 'undefined' ? Range.prototype : null, typeof Element !== 'undefined' ? Element.prototype : null]) {
  if (!proto) continue
  if (typeof (proto as { getClientRects?: unknown }).getClientRects !== 'function') {
    ;(proto as { getClientRects: () => DOMRectList }).getClientRects = emptyRectList
  }
  if (typeof (proto as { getBoundingClientRect?: unknown }).getBoundingClientRect !== 'function') {
    ;(proto as { getBoundingClientRect: () => DOMRect }).getBoundingClientRect = emptyRect
  }
}
