// Rehearse the phone page without leaving the desk: an emulated Pixel walks each
// chosen block by moving its GPS, takes one Can't override (corner 3) and one I'm
// here tap (corner 6), screenshots the readout, then ends the walk.
//
//   python -m jev_walker.server --area sf --chooser jev --port 8765 --token rehearse
//   BASE=http://127.0.0.1:8765 TOKEN=rehearse node tools/phone_rehearsal.js <screenshot dir> [corners]
//
// Needs Node with Playwright and Chromium.
const { chromium, devices } = require('playwright');
const OUT = process.argv[2] || '.';
const BASE = process.env.BASE || 'http://127.0.0.1:8765';
const TOKEN = process.env.TOKEN || 'rehearse';
const CORNERS = Number(process.argv[3] || 12);
const START = (process.env.START || '37.78775,-122.40838').split(',').map(Number);

function along(coords, d) {   // point d metres along [lon,lat] polyline
  const R = 6371008.8, rad = Math.PI / 180;
  const dist = (a, b) => { const x = (b[0]-a[0])*rad*Math.cos((a[1]+b[1])/2*rad), y = (b[1]-a[1])*rad; return R*Math.hypot(x, y); };
  for (let i = 1; i < coords.length; i++) {
    const s = dist(coords[i-1], coords[i]);
    if (d <= s) { const f = s ? d / s : 0; return [coords[i-1][0] + (coords[i][0]-coords[i-1][0])*f, coords[i-1][1] + (coords[i][1]-coords[i-1][1])*f]; }
    d -= s;
  }
  return coords[coords.length - 1];
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ ...devices['Pixel 7'], geolocation: { latitude: START[0], longitude: START[1], accuracy: 6 },
                                         permissions: ['geolocation'] });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('dialog', (d) => d.accept());
  await page.goto(`${BASE}/?t=${TOKEN}&avg=2`);
  await sleep(800);
  if (await page.locator('#newBtn').isVisible()) { await page.click('#newBtn'); await sleep(300); }
  await page.screenshot({ path: `${OUT}/1-idle.png` });
  await page.click('#startBtn');
  await page.waitForFunction(() => !document.getElementById('walking').classList.contains('hidden'), null, { timeout: 15000 });
  await sleep(900);
  await page.screenshot({ path: `${OUT}/2-first-corner.png` });

  const api = async (p) => (await page.request.get(`${BASE}${p}`, { headers: { 'X-Walk-Token': TOKEN } })).json();
  let shots = 0, overrideDone = false, hereDone = false;
  for (let n = 0; n < CORNERS; n++) {
    const log = await api('/api/log');
    const view = await api('/api/view');
    if (view.phase !== 'walking') break;
    const rec = log.decisions[log.decisions.length - 1];
    let taken = (log.pending && log.pending.taken) || rec.choice;
    // Corner 3: say Jev's pick is blocked and take another exit.
    if (n === 3 && !overrideDone) {
      const alt = rec.menu.find((o) => o.kind === 'exit' && o.id !== rec.choice && !o.back);
      if (alt) {
        await page.click('#cant');
        await sleep(300);
        await page.screenshot({ path: `${OUT}/5-cant-sheet.png` });
        await page.locator('#sheetList button', { hasText: `${alt.direction} · ${alt.street || 'path'}` }).first().click();
        await page.locator('#sheetList button', { hasText: 'Sidewalk closed' }).click();
        await sleep(400);
        taken = alt.id; overrideDone = true;
      }
    }
    const opt = rec.menu.find((o) => o.id === taken);
    const before = rec.i;
    // Corner 6: use the I'm here button instead of GPS.
    if (n === 6 && !hereDone) {
      await sleep(3500);
      await page.click('#here'); hereDone = true;
    } else {
      const len = opt.block_length_m;
      for (let d = 0; ; d += 18) {
        const [lon, lat] = along(opt.coords, Math.min(d, len));
        await ctx.setGeolocation({ latitude: lat, longitude: lon, accuracy: 6 });
        await sleep(350);
        if (n === 1 && d >= len * 0.6 && shots === 0) {   // a mid-block frame
          await sleep(15500);
          await ctx.setGeolocation({ latitude: lat + 1e-6, longitude: lon, accuracy: 6 });
          await sleep(1200);
          await page.screenshot({ path: `${OUT}/3-mid-block.png` }); shots++;
        }
        const v = await api('/api/view');
        if (v.phase !== 'walking' || (v.decision && v.decision.i !== before)) break;
        if (d > len + 400) throw new Error('never arrived at corner ' + n);
      }
    }
    await sleep(500);
    if (n === 4) await page.screenshot({ path: `${OUT}/4-later-corner.png` });
  }
  await page.screenshot({ path: `${OUT}/6-before-end.png` });
  await page.click('#endBtn');
  await sleep(800);
  await page.screenshot({ path: `${OUT}/7-ended.png` });
  const final = await api('/api/log');
  console.log(JSON.stringify({ decisions: final.decisions.length, overrides: final.score.overrides,
    arrivals: final.ui_events.filter((e) => e.type === 'arrival').map((e) => e.manual),
    gps_fixes: final.gps.length, end: final.end, offset: final.header.clock_offset_s, errors }, null, 1));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
