import { chromium } from '@playwright/test';
import fs from 'node:fs';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, array) =>
  index % 2 === 0 ? [...pairs, [value.replace(/^--/, ''), array[index + 1]]] : pairs, []));
const headings = { '/': '工作台', '/analysis': '分析与更新', '/history': '历史与复盘', '/settings': '设置' };
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'no-preference' });
const page = await context.newPage();
const pageErrors = [];
const network = [];
page.on('pageerror', error => pageErrors.push(error.message));
page.on('response', response => {
  if (response.url().startsWith(args.api)) network.push({ url: response.url().replace(args.api, ''), status: response.status() });
});
try {
  const workspaceReady = page.waitForResponse(response => response.url().endsWith('/v1/workspace') && response.status() === 200);
  await page.goto(`${args.web}/?api=${encodeURIComponent(args.api)}#token=${encodeURIComponent(args.token)}`, { waitUntil: 'domcontentloaded' });
  await workspaceReady;
  await page.locator('main h1').filter({ hasText: '工作台' }).waitFor();
  // Warm bundled routes and ordinary query caches; measurements below exclude Playwright waits.
  for (const route of ['/analysis', '/history', '/settings', '/']) {
    await page.locator(`nav[aria-label="主导航"] a[href="${route}"]`).click();
    await page.locator('main h1').filter({ hasText: headings[route] }).waitFor();
    await page.waitForTimeout(250);
  }
  await page.evaluate(expected => {
    window.__xauFeedback = { samples: [], timeouts: [], longTasks: [] };
    const probe = window.__xauFeedback;
    if (PerformanceObserver.supportedEntryTypes.includes('longtask')) {
      new PerformanceObserver(entries => {
        for (const entry of entries.getEntries()) probe.longTasks.push({ startTime: entry.startTime, duration: entry.duration });
      }).observe({ type: 'longtask', buffered: false });
    }
    document.addEventListener('click', event => {
      const link = event.target.closest('nav[aria-label="主导航"] a');
      if (!link) return;
      const route = new URL(link.href).pathname;
      if (!expected[route] || route === location.pathname) return;
      const eventClock = event.timeStamp;
      const handlerClock = performance.now();
      const started = Math.abs(handlerClock - eventClock) < 1000 ? eventClock : handlerClock;
      let frames = 0;
      const observe = rafTimestamp => {
        frames += 1;
        const heading = document.querySelector('main h1');
        const visible = heading && heading.getBoundingClientRect().height > 0 && getComputedStyle(heading).visibility !== 'hidden';
        if (location.pathname === route && visible && heading.textContent.trim() === expected[route]) {
          const observed = performance.now();
          probe.samples.push({ route, heading: heading.textContent.trim(), event_timestamp_ms: eventClock,
            handler_timestamp_ms: handlerClock, first_heading_raf_timestamp_ms: rafTimestamp,
            observed_callback_ms: observed, elapsed_ms: observed - started, raf_callbacks: frames,
            event_timestamp_used: started === eventClock, trusted_event: event.isTrusted });
        } else if (performance.now() - started > 5000) {
          probe.timeouts.push({ route, event_timestamp_ms: eventClock });
        } else requestAnimationFrame(observe);
      };
      requestAnimationFrame(observe);
    }, true);
  }, headings);
  for (let index = 0; index < 40; index += 1) {
    const route = ['/analysis', '/history', '/settings', '/'][index % 4];
    await page.locator(`nav[aria-label="主导航"] a[href="${route}"]`).click();
    await page.waitForFunction(count => window.__xauFeedback.samples.length >= count, index + 1);
    await page.waitForTimeout(100);
  }
  const raw = await page.evaluate(() => window.__xauFeedback);
  const sorted = raw.samples.map(sample => sample.elapsed_ms).sort((a, b) => a - b);
  const p95 = sorted[Math.ceil(sorted.length * .95) - 1];
  const result = { kind: 'headless-chrome-local-navigation-feedback', browser_version: browser.version(),
    headless: true, viewport: { width: 1440, height: 1000 }, warmup_route_visits: 4, sample_count: raw.samples.length,
    p50_ms: sorted[Math.ceil(sorted.length * .5) - 1], p95_ms: p95, max_ms: sorted.at(-1), target_ms: 100,
    target_met: p95 <= 100 && raw.samples.length === 40 && raw.samples.every(sample => sample.trusted_event)
      && pageErrors.length === 0 && raw.timeouts.length === 0 && network.every(item => item.status < 400),
    measurement: 'DOM capture-phase trusted click event.timeStamp to first requestAnimationFrame callback observing the destination h1 with visible layout; Playwright click/wait duration is excluded.',
    limits: ['rAF callback runs before paint; this is first-frame readiness, not physical display scanout or browser INP.',
      'Four already loaded routes, warmed query caches, real authenticated local HTTP and production dist; data-fetch completion latency is separate.'],
    raw_samples: raw.samples, timeout_samples: raw.timeouts, long_tasks: raw.longTasks, page_errors: pageErrors,
    api_responses: network, api_error_count: network.filter(item => item.status >= 400).length };
  await page.screenshot({ path: args.screenshot, fullPage: true });
  fs.writeFileSync(args.output, JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ sample_count: result.sample_count, p50_ms: result.p50_ms, p95_ms: result.p95_ms,
    target_met: result.target_met, page_error_count: pageErrors.length, api_error_count: result.api_error_count }));
} finally {
  await context.close();
  await browser.close();
}
