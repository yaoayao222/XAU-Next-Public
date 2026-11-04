// No refresh button clicks: observe live facts arriving through the normal SPA.
const { chromium, expect } = require('@playwright/test');
const { join } = require('node:path');
let phase='setup';
(async () => {
  let input=''; for await(const chunk of process.stdin) input+=chunk;
  const session=JSON.parse(input);
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const faults=[]; page.on('pageerror',e=>faults.push(e.name));
    const nextWorkspace=(previous)=>page.waitForResponse(async r=>{
      if(new URL(r.url()).pathname!=='/v1/workspace' || r.status()!==200) return false;
      const w=await r.json(); return w.market && w.market.snapshot_id!==previous;
    },{timeout:25000}).then(r=>r.json());
    const ready=nextWorkspace(null);
    await page.goto(session.web_url+'/#token='+session.token);
    const first=await ready;
    if(first.market.source_kind!=='mt5' || !first.fact_refresh?.enabled) throw Error('automatic real source missing');
    await expect(page.getByText('自动刷新事实已开启',{exact:true})).toBeVisible();
    await expect(page.locator('.quote-grid .metric').filter({hasText:'BID · 卖出侧'}).locator('strong')).toHaveText(first.market.quote.bid);
    const second=await nextWorkspace(first.market.snapshot_id);
    await expect(page.locator('.quote-grid .metric').filter({hasText:'BID · 卖出侧'}).locator('strong')).toHaveText(second.market.quote.bid);
    await expect(page.locator('.quote-grid .metric').filter({hasText:'ASK · 买入侧'}).locator('strong')).toHaveText(second.market.quote.ask);
    if(second.portfolio.snapshot_id===first.portfolio.snapshot_id) throw Error('portfolio did not advance');
    await page.screenshot({path:join(session.output,'automatic-workspace.png'),fullPage:true});
    await page.getByRole('link',{name:'04 设置',exact:true}).click();
    phase='settings draft input';
    const draft=page.getByLabel(/^默认手数/);
    await draft.fill('0.07');
    phase='automatic update while editing';
    await nextWorkspace(second.market.snapshot_id);
    await expect(draft).toHaveValue('0.07'); // no save: keep real business settings unchanged
    if(faults.length || new URL(page.url()).hash) throw Error('page exception or uncleared fragment');
    process.stdout.write(JSON.stringify({automatic_indicator:true,quote_values_match_api:true,market_and_portfolio_advanced_without_click:true,unsaved_settings_draft_preserved:true,page_errors:0}));
  } finally { await browser.close(); }
})().catch(error=>{process.stderr.write(`Automatic refresh UI verification failed at ${phase}: ${error.name}`);process.exitCode=1;});
