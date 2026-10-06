const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const targetUrl = process.env.TARGET_URL;
const videoDir = path.resolve(process.env.VIDEO_DIR || 'videos');
const skippedPath = /\/(logout|signout|checkout|orders?|delete|remove)(\/|$)/i;

async function main() {
  if (!targetUrl) throw new Error('TARGET_URL is required.');

  const baseUrl = new URL(targetUrl);
  await fs.mkdir(videoDir, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: videoDir, size: { width: 1280, height: 800 } },
  });
  const page = await context.newPage();
  const video = page.video();

  try {
    const visited = new Set([baseUrl.href]);
    try {
      await page.goto(baseUrl.href, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(1500);
    } catch (error) {
      console.warn(`Could not open ${baseUrl.href}: ${error.message}`);
    }

    const links = await page.locator('a[href]').evaluateAll((anchors, origin) => {
      const result = [];
      for (const anchor of anchors) {
        const url = new URL(anchor.href, origin);
        if (url.origin !== origin || url.hash || !['http:', 'https:'].includes(url.protocol)) continue;
        result.push(url.href);
      }
      return result;
    }, baseUrl.origin);

    for (const href of links) {
      const url = new URL(href);
      if (visited.has(href) || skippedPath.test(url.pathname)) continue;
      visited.add(href);
      try {
        await page.goto(href, { waitUntil: 'domcontentloaded', timeout: 15000 });
        await page.waitForTimeout(1000);
      } catch (error) {
        console.warn(`Could not record ${href}: ${error.message}`);
      }
      if (visited.size >= 6) break;
    }
  } finally {
    await context.close();
    await browser.close();
  }

  const generatedPath = await video.path();
  const outputPath = path.join(videoDir, 'qa-navigation.webm');
  await fs.rename(generatedPath, outputPath);
  console.log(`Recorded navigation video: ${outputPath}`);
}

main().catch((error) => {
  console.error('Video recording failed:', error);
  process.exitCode = 1;
});
