import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { Appearance, Behavior } from '../settings/PreferenceSections'
import { useTheme } from '../lib/theme'
import { readUiSize } from '../lib/uiSize'
import { readChatPreferences } from '../lib/chatPreferences'
import { readApplyPolicy } from '../../lib/applyPolicy'
import { S } from '../settings/settingsText'
import { ST } from '../settings/settings.text'

afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })
function Theme() { const theme = useTheme(); return <Appearance theme={theme.choice} onTheme={theme.setChoice} /> }

it('浏览器拒绝保存时，外观和偏好维持原值，恢复存储后可重试并读回', () => {
  localStorage.clear()
  render(<><Theme /><Behavior /></>)
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  fireEvent.click(screen.getByRole('button', { name: S.themes.dark }))
  fireEvent.click(within(screen.getByRole('group', { name: S.uiSize })).getByRole('button', { name: S.uiSizes.xl }))
  fireEvent.click(screen.getByRole('switch', { name: S.behavior.thinking }))
  fireEvent.click(screen.getByRole('button', { name: ST.savePolicyReview }))
  expect(screen.getAllByRole('alert')).toHaveLength(4)
  expect(screen.getByRole('button', { name: S.themes.system })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('switch', { name: S.behavior.thinking })).toHaveAttribute('aria-checked', 'true')
  expect(readUiSize()).toBe('l')
  expect(readApplyPolicy()).toBe('auto')
  expect(readChatPreferences().expandThinking).toBe(true)
  write.mockRestore()
  fireEvent.click(screen.getByRole('button', { name: S.themes.dark }))
  fireEvent.click(within(screen.getByRole('group', { name: S.uiSize })).getByRole('button', { name: S.uiSizes.xl }))
  fireEvent.click(screen.getByRole('switch', { name: S.behavior.thinking }))
  fireEvent.click(screen.getByRole('button', { name: ST.savePolicyReview }))
  expect(screen.queryAllByRole('alert')).toHaveLength(0)
  cleanup()
  render(<><Theme /><Behavior /></>)
  expect(screen.getByRole('button', { name: S.themes.dark })).toHaveAttribute('aria-pressed', 'true')
  expect(readUiSize()).toBe('xl')
  expect(readApplyPolicy()).toBe('review')
  expect(screen.getByRole('switch', { name: S.behavior.thinking })).toHaveAttribute('aria-checked', 'false')
})
