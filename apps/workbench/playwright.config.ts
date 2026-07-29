import { defineConfig } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const appRoot = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(appRoot, '../..')
const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL
const runtimePort = Number(process.env.PLAYWRIGHT_RUNTIME_PORT ?? 8789)
const providerPort = Number(process.env.PLAYWRIGHT_PROVIDER_PORT ?? 18790)
const workbenchPort = Number(process.env.PLAYWRIGHT_WORKBENCH_PORT ?? 5176)
const runtimeUrl = `http://127.0.0.1:${runtimePort}`
const workbenchUrl = `http://127.0.0.1:${workbenchPort}`
const fixtureRoot = path.join(
  repoRoot,
  'services/writing-runtime/tests/fixtures/minimal-novel',
)
const fixtureParent = path.dirname(fixtureRoot)
const localSourceFixtureRoot = path.join(
  repoRoot,
  'services/writing-runtime/tests/fixtures/local-source',
)

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  retries: 0,
  workers: 1,
  reporter: [['line']],
  use: {
    baseURL: externalBaseUrl ?? workbenchUrl,
    channel: 'chrome',
    trace: 'retain-on-failure',
  },
  webServer: externalBaseUrl
    ? undefined
    : [
        {
          command: `"${path.join(repoRoot, '.venv/Scripts/python.exe')}" "${path.join(repoRoot, 'services/writing-runtime/tests/fake_agent_provider.py')}"`,
          cwd: repoRoot,
          url: `http://127.0.0.1:${providerPort}/health`,
          timeout: 30_000,
          reuseExistingServer: false,
        },
        {
          command: `"${path.join(repoRoot, '.venv/Scripts/python.exe')}" -m zhimeng_runtime`,
          cwd: repoRoot,
          env: {
            ...process.env,
            PYTHONPATH: path.join(repoRoot, 'services/writing-runtime/src'),
            ZHIMENG_ENVIRONMENT: 'e2e',
            ZHIMENG_HOST: '127.0.0.1',
            ZHIMENG_PORT: String(runtimePort),
            ZHIMENG_DATABASE_PATH: path.join(
              repoRoot,
              `.runtime/e2e-${process.pid}/runtime.sqlite3`,
            ),
            ZHIMENG_TRUSTED_PROJECT_ROOTS: fixtureParent,
            ZHIMENG_TRUSTED_SOURCE_ROOTS: localSourceFixtureRoot,
            ZHIMENG_ALLOWED_ORIGINS: workbenchUrl,
            // Keep browser E2E deterministic and independent of user-level Skills.
            ZHIMENG_DISTILLATION_SKILL_ROOT: '',
            ZHIMENG_DISTILLATION_LIBRARY: '',
            ZHIMENG_AGENT_PROVIDER_BASE_URL: `http://127.0.0.1:${providerPort}/v1`,
            ZHIMENG_AGENT_PROVIDER_MODEL: 'playwright-scripted-model',
          },
          url: `${runtimeUrl}/health/ready`,
          timeout: 30_000,
          reuseExistingServer: false,
        },
        {
          command: `"${process.execPath}" "${path.join(appRoot, 'node_modules/vite/bin/vite.js')}" --host 127.0.0.1 --port ${workbenchPort} --strictPort --force`,
          cwd: appRoot,
          env: {
            ...process.env,
            VITE_RUNTIME_URL: runtimeUrl,
            VITE_PROJECT_ROOT: fixtureRoot,
          },
          url: workbenchUrl,
          timeout: 30_000,
          reuseExistingServer: false,
        },
      ],
})
