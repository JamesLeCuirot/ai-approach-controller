// Record a radar replay as a video and take a screenshot of its final frame.
//
//   node scripts/record_replay.cjs <replay.html> <video.webm> <screenshot.png> [speed]
//
// Uses the Playwright that is installed with Node. The replay plays itself
// (autoplay) and sets window.replayDone when it reaches the end.
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright');

(async () => {
  const [html, videoOut, shotOut, speedArg] = process.argv.slice(2);
  if (!html || !videoOut || !shotOut) {
    console.error('usage: node scripts/record_replay.cjs <replay.html> <video.webm> <screenshot.png> [speed]');
    process.exit(2);
  }
  const url = 'file://' + path.resolve(html);
  const speed = Number(speedArg || 10);
  const size = { width: 1280, height: 720 };
  const browser = await chromium.launch();

  // 1. Video of the replay playing at the chosen speed.
  const videoDir = fs.mkdtempSync(path.join(path.dirname(path.resolve(videoOut)), '.rec-'));
  const ctx = await browser.newContext({ viewport: size, recordVideo: { dir: videoDir, size } });
  const page = await ctx.newPage();
  await page.goto(`${url}?autoplay=1&speed=${speed}&delay=1500`);
  const duration = await page.evaluate(() => window.replayDuration || 10);
  await page.waitForFunction(() => window.replayDone === true, null, { timeout: (duration / speed) * 1000 + 60000 });
  await page.waitForTimeout(2500); // hold the final frame for a moment
  const video = page.video();
  await ctx.close();
  fs.mkdirSync(path.dirname(path.resolve(videoOut)), { recursive: true });
  fs.renameSync(await video.path(), videoOut);
  fs.rmSync(videoDir, { recursive: true, force: true });

  // 2. Sharp screenshot of the final frame for diaries and thumbnails.
  const shotCtx = await browser.newContext({ viewport: size, deviceScaleFactor: 2 });
  const shot = await shotCtx.newPage();
  await shot.goto(`${url}?autoplay=1&speed=100000&delay=0&trail=all`);
  await shot.waitForFunction(() => window.replayDone === true, null, { timeout: 60000 });
  await shot.waitForTimeout(300);
  await shot.screenshot({ path: shotOut });
  await shotCtx.close();
  await browser.close();
  console.log(JSON.stringify({ video: videoOut, screenshot: shotOut, duration, speed }));
})().catch(err => { console.error(err); process.exit(1); });
