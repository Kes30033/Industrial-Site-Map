// Smoke test: serve the built site, click through it in headless Chromium, save screenshots + console errors.
// Usage: node scripts/qa.mjs <site-dir> <out-dir>
import { chromium } from 'playwright';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const siteDir = path.resolve(process.argv[2] || 'site');
const outDir = path.resolve(process.argv[3] || 'qa');
fs.mkdirSync(outDir, { recursive: true });
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };
const server = http.createServer((req, res) => {
  const p = path.join(siteDir, decodeURIComponent(req.url.split('?')[0].split('#')[0]).replace(/\/$/, '/index.html'));
  fs.readFile(p, (err, buf) => {
    if (err) { res.writeHead(404); res.end('not found'); return; }
    res.writeHead(200, { 'Content-Type': types[path.extname(p)] || 'application/octet-stream' });
    res.end(buf);
  });
}).listen(8765);

const log = [];
const browser = await chromium.launch();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function session(name, opts, steps) {
  const ctx = await browser.newContext(opts);
  const page = await ctx.newPage();
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) log.push(`[${name}] console.${m.type()}: ${m.text()}`); });
  page.on('pageerror', (e) => log.push(`[${name}] pageerror: ${e.message}`));
  const tileFails = {};
  page.on('requestfailed', (r) => {
    const u = new URL(r.url());
    if (u.pathname.includes('/tile/')) { tileFails[u.host] = (tileFails[u.host] || 0) + 1; return; }
    log.push(`[${name}] requestfailed: ${r.url()} ${r.failure()?.errorText}`);
  });
  page.on('response', (r) => { if (r.status() >= 400) log.push(`[${name}] HTTP ${r.status()}: ${r.url().slice(0, 140)}`); });
  const t0 = Date.now();
  try {
    await page.goto('http://localhost:8765/', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#loading', { state: 'hidden', timeout: 45000 });
    log.push(`[${name}] ready in ${Date.now() - t0} ms`);
    await sleep(2500);
    await steps(page);
  } catch (e) {
    log.push(`[${name}] failed: ${e.message.split('\n')[0]}`);
    await page.screenshot({ path: path.join(outDir, `${name}-failed.png`) }).catch(() => {});
  } finally {
    if (Object.keys(tileFails).length) log.push(`[${name}] failed tile requests: ${JSON.stringify(tileFails)}`);
    fs.writeFileSync(path.join(outDir, 'log.txt'), log.join('\n') + '\n');
    await ctx.close();
  }
}

const shot = (page, n, full = false) => page.screenshot({ path: path.join(outDir, `${n}.png`), fullPage: full });

await session('desktop', { viewport: { width: 1440, height: 900 }, colorScheme: 'light' }, async (page) => {
  await shot(page, '01-desktop-default');
  const top = await page.$$eval('.rank-item', (els) => els.slice(0, 10).map((e) => e.innerText.replace(/\s+/g, ' ')));
  log.push('[desktop] default weights top 10: ' + JSON.stringify(top));
  const links = await page.$$eval('.wmeta a', (els) => els.length);
  log.push(`[desktop] source links under sliders: ${links}`);
  await page.check('#per-state');
  await sleep(500);
  const ps = await page.$$eval('.rank-item', (els) => els.slice(0, 12).map((e) => e.innerText.replace(/\s+/g, ' ')));
  log.push('[desktop] best per state: ' + JSON.stringify(ps));
  await shot(page, '02-desktop-per-state');
  await page.uncheck('#per-state');
  // timing of a weight change
  const ms = await page.evaluate(async () => {
    const el = document.querySelector('#w-port');
    const t = performance.now();
    el.value = 2; el.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    return performance.now() - t;
  });
  log.push(`[desktop] weight change -> repaint ${ms.toFixed(0)} ms`);
  await page.click('.rank-item');
  await sleep(2500);
  await shot(page, '03-desktop-county-detail');
  await page.click('input[data-layer="rail"]');
  await page.click('input[data-layer="interstates"]');
  await page.click('input[data-layer="yards"]');
  await page.click('input[data-layer="airports"]');
  await sleep(2500);
  await shot(page, '04-desktop-all-layers');
  await page.selectOption('#f-state', 'TX');
  await page.selectOption('#f-rail', '5');
  await sleep(1800);
  await shot(page, '05-desktop-texas-rail');
  await page.click('#open-about');
  await sleep(400);
  await shot(page, '06-about');
  log.push('[desktop] url: ' + page.url());
});

await session('dark', { viewport: { width: 1280, height: 820 }, colorScheme: 'dark' }, async (page) => {
  const r = await page.$('.rank-item');
  await r.click();
  await sleep(1800);
  await shot(page, '07-dark-detail');
});

await session('mobile', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }, async (page) => {
  await shot(page, '08-mobile-top');
  await shot(page, '09-mobile-full', true);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  log.push(`[mobile] horizontal overflow: ${overflow}px`);
});

await browser.close();
server.close();
fs.writeFileSync(path.join(outDir, 'log.txt'), log.join('\n') + '\n');
console.log(log.join('\n'));
