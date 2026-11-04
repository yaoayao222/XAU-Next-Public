"""One shared public-source capture; no model/search API, key, retry or MT5."""
import hashlib,json,re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from decimal import Decimal
import httpx

from xau_next.shared.research_sources import PUBLIC_SOURCES,TOPICS,treasury_url,valid_source_url
from xau_next.shared.values import utc_now,parse_utc
from .research import MAX_BYTES,fetch_public_research
from .official import bls_calendar,bea_calendar,bls_release,bea_release,feed_news,daily_yields


def fetch_source(item,*,transport,clock):
    name,url=item;raw=None
    try:
        with httpx.Client(transport=transport,timeout=20,follow_redirects=False) as client:
            with client.stream('GET',url,headers={'User-Agent':'XAU-Next-Public-Research/2.0','Accept':'*/*'}) as response:
                response.raise_for_status();chunks=[];size=0
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>MAX_BYTES:raise ValueError('response too large')
                    chunks.append(chunk)
                raw=b''.join(chunks)
        return name,{'source_id':name,'url':url,'captured_at':clock(),'status':'available',
          'content_sha256':hashlib.sha256(raw).hexdigest(),'message':'已取得公开来源，评级与实际值分别核验'},raw
    except Exception as exc:
        return name,{'source_id':name,'url':url,'captured_at':clock(),'status':'unavailable','content_sha256':None,
          'message':'来源暂未取得；不影响其他来源，本次不自动重试。','diagnostic':type(exc).__name__},raw


def dollar_series(raw,*,source):
    value=json.loads(raw)['chart']['result'][0]
    if value['meta'].get('symbol')!='DX-Y.NYB' or value['meta'].get('instrumentType')!='INDEX':raise ValueError('wrong index')
    stamps=value.get('timestamp') or []
    if not stamps:return []
    prices=value['indicators']['quote'][0]['close']
    if len(stamps)!=len(prices) or len(stamps)>3000:raise ValueError('series lengths')
    points=[];seen=set()
    for stamp,number in zip(stamps,prices):
        if number is None:continue
        if type(stamp) is not int:raise ValueError('series timestamp')
        at=datetime.fromtimestamp(stamp,timezone.utc).isoformat().replace('+00:00','Z')
        if parse_utc(at)>parse_utc(source['captured_at']) or at in seen:raise ValueError('series future/duplicate')
        seen.add(at);text=str(number)
        if not re.fullmatch(r'-?\d+(?:\.\d+)?',text) or not Decimal(text).is_finite():raise ValueError('series value')
        points.append({'symbol':'DXY','value':text,'unit':'指数点','observed_at':at,'captured_at':source['captured_at'],
          'source_id':'DXY','content_sha256':source['content_sha256'],'real_time_verified':False})
    return sorted(points,key=lambda p:p['observed_at'])


def fetch_multi_source_research(*,month,transport=None,clock=utc_now):
    if not re.fullmatch(r'20\d{2}-(?:0[1-9]|1[0-2])',month):raise ValueError('invalid research month')
    # Month selects calendar scope. Current Treasury observations use capture year,
    # not an arbitrary historical month selected in the calendar control.
    urls={**PUBLIC_SOURCES,'treasury-daily':treasury_url(parse_utc(clock()).year)}
    def extra():
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(lambda item:fetch_source(item,transport=transport,clock=clock),urls.items()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        prior=pool.submit(fetch_public_research,month=month,transport=transport,clock=clock)
        newer=pool.submit(extra)
        value,raws=prior.result();captures=newer.result()
    releases=[];news=[];rates=[];latest=[]
    for name,source,raw in captures:
        raws[name]=raw
        if source['status']=='available':
            try:
                args={'captured_at':source['captured_at']}
                if name=='bls-calendar':value['events'].extend(bls_calendar(raw,month=month,**args))
                elif name=='bea-calendar':
                    rows,latest=bea_calendar(raw,month=month,**args);value['events'].extend(rows)
                elif name in {'bls-cpi','bls-ppi','bls-jobs'}:
                    releases.append(bls_release(raw,source_id=name,url=source['url'],**args))
                elif name.startswith(('fed-','bbc-')):news.extend(feed_news(raw,source_id=name,**args))
                elif name=='treasury-daily':rates=daily_yields(raw,url=source['url'],**args)
            except Exception as exc:
                source.update(status='unavailable',message='公开格式或时间未通过核验；缺口保留，不阻断其他来源。',diagnostic=type(exc).__name__)
        value['sources'].append(source)
    # At most one GDP and one PCE page, followed only from the official calendar
    # after its scheduled time; exact origin/path allowlist, no redirects/retries.
    for row in latest:
        if not valid_source_url(row['source_id'],row['url']):continue
        name,source,raw=fetch_source((row['source_id'],row['url']),transport=transport,clock=clock)
        raws[name]=raw
        if source['status']=='available':
            try:releases.append(bea_release(raw,source_id=name,url=row['url'],captured_at=source['captured_at'],
                  scheduled_at=row['scheduled_at'],title=row['title']))
            except Exception as exc:source.update(status='unavailable',message='官方报告身份、月份或日期未能核验，未猜测实际值。',diagnostic=type(exc).__name__)
        value['sources'].append(source)
    unique={}
    for row in news:
        # Same article appearing in multiple feeds remains one shared item.
        unique.setdefault(row['url'],row)
    series=[]
    dxy=next((s for s in value['sources'] if s['source_id']=='DXY'),None)
    series_message='没有取得可核验的美元分钟序列；单个报价不证明连续实时。'
    if dxy and dxy['status']=='available' and raws.get('DXY'):
        try:
            series=dollar_series(raws['DXY'],source=dxy)
            if series:series_message='取得公开美元观测点，仍需检查开市更新、间隔与延迟，未认证连续实时。'
        except Exception:series_message='美元序列格式或时间未核验；保留单点报价，未补造序列。'
    at=clock()
    value.update(version='macro-research/2',captured_at=at,
      scope='多来源共同资料：金十可选；BLS/BEA官方日程与已公布报告，央行/公开新闻检索及带时间报价',
      state='partial',official_releases=releases,news=sorted(unique.values(),key=lambda n:n['published_at'] or '',reverse=True),
      reference_rates=rates,quote_series=series,search={'mode':'shared_public_sources','searched_at':at,
        'topics':TOPICS,'native_model_search':False,'paid_search_requests':0,
        'note':'程序检索固定官方与公开RSS来源，所有模型共享；不是全网搜索、新闻全文核验或已执行模型搜索。'},
      limitations=[
        '金十为可选来源，无法连接不阻断BLS、BEA及其他公开资料；官方无金十星级，不换算。',
        '官方实际值与市场预期独立保存，字段不明确、尚未公布或时期不符时不补数。',
        '新闻来自固定公开来源的标题/摘要检索，覆盖不完整；突发消息可能遗漏，关键词不决定金价方向。',
        '已知未来日程仅表示将公布什么；未知实际值、讲话内容和突发事件不提前编造。',series_message,
        '财政部日度曲线值仅作独立参考，不当作盘中收益率；公开报价连续实时性未认证。'])
    return value,raws
