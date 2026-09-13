import { defineConfig, devices } from '@playwright/test'

/**
 * Browser product flows (see .claude/skills/verify-faultline/features/web-ui.md).
 * Defaults target the deployed Modal Server; override with FAULTLINE_WEB_URL.
 * Uses the installed Google Chrome (channel "chrome") so no browser download is needed;
 * PW_CHANNEL=chromium after `pnpm exec playwright install chromium` if Chrome is absent.
 */
const baseURL = process.env.FAULTLINE_WEB_URL ?? 'https://appliedlabsai-local--faultline-web-site.us-east.modal.direct'

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['json', { outputFile: process.env.PW_JSON_OUT ?? 'e2e-results/results.json' }]],
  outputDir: process.env.PW_OUTPUT_DIR ?? 'e2e-results/artifacts',
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 900 },
    colorScheme: 'dark',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
    actionTimeout: 20_000,
    navigationTimeout: 60_000,
  },
  projects: [
    {
      name: 'chrome',
      use: {
        ...devices['Desktop Chrome'],
        channel: process.env.PW_CHANNEL ?? 'chrome',
        viewport: { width: 1440, height: 900 },
      },
    },
  ],
})
