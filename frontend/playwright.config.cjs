const { defineConfig, devices } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:8080',
    trace: 'retain-on-failure'
  },
  projects: [
    { name: 'chromium-mobile', use: { ...devices['Pixel 5'], browserName: 'chromium' } },
    { name: 'firefox-desktop', use: { browserName: 'firefox', viewport: { width: 1366, height: 768 } } }
  ],
  workers: 1
});
