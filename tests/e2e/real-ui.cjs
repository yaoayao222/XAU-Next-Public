// Local headless UI acceptance. Session token travels only through stdin and browser fragment.
const { chromium, expect } = require('@playwright/test');
const { join } = require('node:path');
(async()=>{
  let input=''; for await(const chunk of process.stdin) input+=chunk;
  const session=JSON.parse(input);
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const faults=[]; page.on('pageerror',e=>faults.push(e.name));
    const responseReady=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/workspace' && r.status()===200);
    await page.goto(session.web_url+'/#token='+session.token);
    const workspace=await (await responseReady).json();
    if(workspace.market?.source_kind!=='mt5' || !workspace.market.quote) throw Error('missing real quote');
    await page.getByRole('heading',{name:'工作台',exact:true}).waitFor();
    await page.getByRole('heading',{name:'XAUUSD 当前事实',exact:true}).waitFor();
    await expect(page.locator('.quote-grid .metric').filter({hasText:'BID · 卖出侧'}).locator('strong')).toHaveText(workspace.market.quote.bid);
    await expect(page.locator('.quote-grid .metric').filter({hasText:'ASK · 买入侧'}).locator('strong')).toHaveText(workspace.market.quote.ask);
    await page.getByText('合约与 K 线来源',{exact:true}).click();
    await expect(page.getByText(`账户来源标识：${workspace.market.account_scope_id} · 数据来源：mt5`,{exact:true})).toBeVisible();
    await page.getByText('合约与 K 线来源',{exact:true}).click();
    if(await page.getByText('合成样例数据 · 用于功能验收，不是实际市场报价').count()) throw Error('wrong source');
    if(new URL(page.url()).hash) throw Error('token fragment not cleared');
    await page.screenshot({path:join(session.output,'real-workspace.png'),fullPage:true});
    await page.getByRole('link',{name:'02 分析与更新',exact:true}).click();
    await page.getByRole('button',{name:/新一轮分析/}).first().click();
    await page.getByRole('button',{name:'复制完整分析包',exact:true}).waitFor();
    await page.screenshot({path:join(session.output,'pending-real-analysis.png'),fullPage:true});
    if(faults.length) throw Error('page exception');
    process.stdout.write(JSON.stringify({real_workspace_visible:true,quote_values_match_api:true,account_scope_match_api:true,pending_request_visible:true,fragment_removed:true,page_errors:0}));
  } finally { await browser.close(); }
})().catch(()=>{process.stderr.write('Local real UI verification failed');process.exitCode=1;});
