// Deterministic frame renderer. Advances the scene by exact frame intervals and
// screenshots each one, so frame N is a pure function of N. No RAF, no clock reads.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i === -1 ? def : process.argv[i + 1];
}
const SCENE = arg('scene'), TIMELINE = arg('timeline'), OUT = arg('out');
const FPS = parseInt(arg('fps', '30'), 10);
const PROBE = process.argv.includes('--probe');
const W = parseInt(arg('width', '1920'), 10), H = parseInt(arg('height', '1080'), 10);

(async () => {
  const beats = JSON.parse(fs.readFileSync(TIMELINE, 'utf8'));
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ args: ['--no-sandbox', '--hide-scrollbars'] });
  const page = await browser.newPage({
    viewport: { width: W, height: H }, deviceScaleFactor: 1,
  });
  await page.goto('file://' + path.resolve(SCENE));
  await page.evaluate((b) => window.setTimeline(b), beats);
  const stage = page.locator('#stage');

  if (PROBE) {
    let acc = 0;
    for (const b of beats) {
      await page.evaluate((t) => window.drawFrame(t), acc + b.seconds * 0.80);
      await stage.screenshot({ path: path.join(OUT, `probe_${b.id}.png`) });
      acc += b.seconds;
    }
    console.log(JSON.stringify({ mode: 'probe', beats: beats.length }));
  } else {
    const total = Math.round(beats.reduce((s, b) => s + b.seconds, 0) * FPS);
    for (let i = 0; i < total; i++) {
      await page.evaluate((t) => window.drawFrame(t), i / FPS);
      await stage.screenshot({
        path: path.join(OUT, 'f' + String(i).padStart(6, '0') + '.png'),
      });
      if (i % 300 === 0) process.stderr.write(`frame ${i}/${total}\n`);
    }
    console.log(JSON.stringify({ mode: 'frames', frames: total, fps: FPS }));
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
