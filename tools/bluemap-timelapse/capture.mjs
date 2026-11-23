import fs from 'fs';
import { chromium } from 'playwright-core';

const TARGET_URL = process.env.BM_URL || 'http://192.168.1.148:8123/#overworld:-6073:49:-3161:211:-2.42:0.96:0:0:perspective';
const OUT = process.env.OUT_PATH || '/out/timelapse-test.png';
const WIDTH = Number(process.env.WIDTH || 1920);
const HEIGHT = Number(process.env.HEIGHT || 1080);
const SUPER_SAMPLING = Number(process.env.SUPER_SAMPLING || 2);
const SUNLIGHT = Number(process.env.SUNLIGHT ?? 0.25);
const HIRES = Number(process.env.HIRES || 200);
const LOWRES = Number(process.env.LOWRES || 7000);

const t0 = Date.now();
const browser = await chromium.launch({
  headless: true,
  args: [
    '--use-gl=angle',
    '--use-angle=swiftshader',
    '--enable-webgl',
    '--enable-unsafe-swiftshader',
    '--ignore-gpu-blocklist',
    '--no-sandbox',
    '--disable-dev-shm-usage'
  ]
});

const context = await browser.newContext({
  viewport: { width: WIDTH, height: HEIGHT },
  deviceScaleFactor: 1
});

const page = await context.newPage();

// 1. Pre-populate BlueMap 5.x localStorage settings (2x superSampling, max hires/lowres)
// 2. Force preserveDrawingBuffer: true for clean WebGL canvas readback
await page.addInitScript(({ hires, lowres, ss }) => {
  try {
    localStorage.setItem('bluemap-hiresViewDistance', String(hires));
    localStorage.setItem('bluemap-lowresViewDistance', String(lowres));
    localStorage.setItem('bluemap-superSampling', String(ss));
  } catch (e) {}

  const origGetContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, attrs) {
    if (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl') {
      attrs = Object.assign({}, attrs || {}, { preserveDrawingBuffer: true });
    }
    return origGetContext.call(this, type, attrs);
  };
}, { hires: HIRES, lowres: LOWRES, ss: SUPER_SAMPLING });

let inflight = 0;
let totalTileReqs = 0;
let lastActivity = Date.now();
const errors = [];

page.on('pageerror', e => errors.push(e.message));
page.on('request', req => {
  if (req.url().includes('/tiles/')) {
    inflight++;
    totalTileReqs++;
    lastActivity = Date.now();
  }
});
page.on('requestfinished', req => {
  if (req.url().includes('/tiles/')) {
    inflight = Math.max(0, inflight - 1);
    lastActivity = Date.now();
  }
});
page.on('requestfailed', req => {
  if (req.url().includes('/tiles/')) {
    inflight = Math.max(0, inflight - 1);
    lastActivity = Date.now();
  }
});

console.log('Opening BlueMap:', TARGET_URL);
await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForSelector('canvas[data-engine*="three"]', { timeout: 30000 });

// Apply BlueMap 2x resolution & dimmed sunlight (0.25) directly via Vue $bluemap instance
const applied = await page.evaluate(({ ss, sunlight }) => {
  const app = document.querySelector('#app')?.__vue_app__;
  const bm = app?.config?.globalProperties?.$bluemap;
  if (!bm || !bm.mapViewer) return { ok: false };
  bm.mapViewer.superSampling = ss;
  const sl = bm.mapViewer.data?.uniforms?.sunlightStrength || bm.mapViewer.uniforms?.sunlightStrength; if (sl) sl.value = sunlight;
  bm.mapViewer.redraw();
  return {
    ok: true,
    superSampling: bm.mapViewer.superSampling,
    sunlight: (bm.mapViewer.data?.uniforms?.sunlightStrength || bm.mapViewer.uniforms?.sunlightStrength)?.value
  };
}, { ss: SUPER_SAMPLING, sunlight: SUNLIGHT });
console.log('Applied settings:', JSON.stringify(applied));

// Wait until tiles settle (quiet for 5s after tiles load, max 45s)
const waitStart = Date.now();
while (Date.now() - waitStart < 45000) {
  await new Promise(r => setTimeout(r, 1000));
  const quietMs = Date.now() - lastActivity;
  const elapsed = Date.now() - waitStart;
  if (totalTileReqs > 0 && elapsed >= 8000 && inflight === 0 && quietMs >= 5000) {
    break;
  }
}

// Re-assert dimmed sunlight (in case map load reset skyLight) and trigger redraw
await page.evaluate(({ ss, sunlight }) => {
  const bm = document.querySelector('#app')?.__vue_app__?.config?.globalProperties?.$bluemap;
  if (bm?.mapViewer) {
    bm.mapViewer.superSampling = ss;
    const sl = bm.mapViewer.data?.uniforms?.sunlightStrength || bm.mapViewer.uniforms?.sunlightStrength; if (sl) sl.value = sunlight;
    bm.mapViewer.redraw();
  }
}, { ss: SUPER_SAMPLING, sunlight: SUNLIGHT });

// Wait 2 WebGL frames for the final dimmed 2x render
await page.evaluate(() => new Promise(resolve => {
  let frames = 0;
  function step() {
    if (++frames >= 2) resolve();
    else requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}));

const result = await page.evaluate(() => {
  const canvases = [...document.querySelectorAll('canvas')];
  const main = canvases.find(c => c.width >= 1000 && c.height >= 500) || canvases[0];
  const bm = document.querySelector('#app')?.__vue_app__?.config?.globalProperties?.$bluemap;
  if (!main) return { error: 'no canvas' };
  return {
    w: main.width,
    h: main.height,
    ss: bm?.mapViewer?.superSampling ?? null,
    sunlight: bm?.mapViewer?.uniforms?.sunlightStrength?.value ?? null,
    dataUrl: main.toDataURL('image/png')
  };
});

if (result.error) throw new Error(result.error);
const buf = Buffer.from(result.dataUrl.replace(/^data:image\/png;base64,/, ''), 'base64');
fs.writeFileSync(OUT, buf);
console.log('Saved:', OUT, 'size:', Math.round(buf.length / 1024) + 'KB', 'tiles:', totalTileReqs, 'elapsedMs:', Date.now() - t0, 'canvas:', result.w + 'x' + result.h, 'ss:', result.ss, 'sunlight:', result.sunlight, 'errors:', errors.slice(0, 3));
await browser.close();
