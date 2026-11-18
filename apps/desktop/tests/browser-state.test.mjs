import test from 'node:test'
import assert from 'node:assert/strict'
import { PageControl } from '../src/browser-state.mjs'

test('handoff invalidates queued observations, including after returning to Agent',()=>{
  const state=new PageControl();state.transfer('agent');const first=state.observe()
  state.require(first.observation_id)
  state.transfer('user')
  assert.throws(()=>state.require(first.observation_id),{code:'user_control'})
  state.transfer('agent')
  assert.throws(()=>state.require(first.observation_id),{code:'stale_observation'})
  const second=state.observe();state.require(second.observation_id)
  state.invalidate()
  assert.throws(()=>state.require(second.observation_id),{code:'stale_observation'})
})
