const { defineConfig, devices } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:8080',
    trace: 'retain-on-failure'
  },
  projects: [
    {
      name: 'chromium-mobile-375',
      use: { ...devices['Pixel 5'], browserName: 'chromium',
        viewport: { width: 375, height: 812 } }
    },
    { name: 'chrome-desktop', use: { browserName: 'chromium', channel: 'chrome',
      viewport: { width: 1366, height: 768 } } },
    { name: 'edge-desktop', use: { browserName: 'chromium', channel: 'msedge',
      viewport: { width: 1366, height: 768 } } },
    { name: 'firefox-desktop', use: { browserName: 'firefox',
      viewport: { width: 1366, height: 768 } } }
  ],
  workers: 1
});
