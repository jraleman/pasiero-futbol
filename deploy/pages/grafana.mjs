// Grafana's Match analysis board for each match, dark and light, as JPEGs for the static site (record.py):
//   node deploy/pages/grafana.mjs <grafana url> <out dir> <match id>...
// Grafana must let anonymous viewers in. NODE_PATH must find Playwright.
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const [grafana, out, ...matches] = process.argv.slice(2);
mkdirSync(out, { recursive: true });

// CHROMIUM: a browser of your own instead of Playwright's download (e.g. /usr/bin/chromium)
const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
// Tall enough for the whole board, so every panel loads; cut at its last panel
const page = await browser.newPage({ viewport: { width: 1440, height: 4200 }, deviceScaleFactor: 1 });
for (const theme of ["dark", "light"]) {
  for (const match of matches) {
    const url = `${grafana}/d/match-analysis/?var-match=${encodeURIComponent(match)}&kiosk&theme=${theme}&from=now-60d&to=now`;
    await page.goto(url, { waitUntil: "networkidle" });
    await page.waitForTimeout(4000);
    const bottom = await page.evaluate(() =>
      Math.max(...[...document.querySelectorAll(".react-grid-item")].map(p => p.getBoundingClientRect().bottom)));
    await page.screenshot({ path: `${out}/${match}.${theme}.jpg`, type: "jpeg", quality: 82,
                            clip: { x: 0, y: 0, width: 1440, height: Math.ceil(bottom) + 16 } });
    console.log(`grafana: ${match} ${theme} (${Math.ceil(bottom)} px)`);
  }
}
await browser.close();
