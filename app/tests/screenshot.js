// Screenshot one page of the running app: node screenshot.js URL OUT [SELECTOR] [dark]
const { chromium } = require('playwright');

(async () => {
  const [url, out, selector, dark] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2,
    // Stand-in for Render's authentication, which adds the token to every request.
    extraHTTPHeaders: process.env.PGM_TOKEN ? { [process.env.PGM_AUTH_HEADER]: process.env.PGM_TOKEN } : {},
    colorScheme: dark ? 'dark' : 'light',
  });
  page.on('pageerror', (e) => { console.error('page error:', e); process.exitCode = 1; });
  await page.goto(url);
  if (selector) await page.waitForSelector(selector, { timeout: 20000 });
  await page.waitForTimeout(1600);
  await page.screenshot({ path: out, fullPage: true });
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
