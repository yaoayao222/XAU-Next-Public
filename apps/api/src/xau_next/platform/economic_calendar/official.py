"""Conservative official calendar/release and public feed parsers; no I/O."""
import calendar
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from xau_next.shared.values import parse_utc
from xau_next.shared.research_sources import NEWS_WORDS, valid_article_url

MONTHS={name.lower():i for i,name in enumerate(calendar.month_name) if name}
EASTERN=ZoneInfo('America/New_York')
Z=lambda dt:dt.astimezone(timezone.utc).isoformat().replace('+00:00','Z')


class Plain(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.skip+=1
        if tag in {'p','div','tr','li','br','h1','h2','h3'} and not self.skip:self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.skip=max(0,self.skip-1)
        if tag in {'p','div','tr','li','h1','h2','h3'} and not self.skip:self.parts.append('\n')
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)


def plain(value):
    p=Plain();p.feed(value)
    return re.sub(r'\s+',' ',' '.join(p.parts)).strip()


def html_document(raw):
    value=raw.decode('utf-8-sig')
    value=re.sub(r'<!--.*?-->','',value,flags=re.S)
    return re.sub(r'<(script|style)\b[^>]*>.*?</\1\s*>','',value,flags=re.S|re.I)


def identity(*values):return hashlib.sha256('|'.join(str(v) for v in values).encode()).hexdigest()[:24]


def event(source_id,title,at,captured_at,reference=None):
    return {'event_id':source_id+'-'+identity(title,at),'title':title,'country':'美国','currency':'USD',
      'scheduled_at':at,'reference_period':reference,'kind':'data','importance':None,
      'importance_scale':'官方日程，无市场星级；不换算为金十评级','previous':None,'forecast':None,'actual':None,
      'revised':None,'unit':None,'actual_available_at':None,'source_id':source_id,
      'status':'scheduled' if parse_utc(at)>parse_utc(captured_at) else 'value_missing'}


def bls_calendar(raw,*,month,captured_at):
    value=raw.decode('utf-8-sig')
    if not value.startswith('BEGIN:VCALENDAR') or 'END:VCALENDAR' not in value:raise ValueError('not a calendar')
    value=re.sub(r'\r?\n[ \t]','',value)
    blocks=re.findall(r'BEGIN:VEVENT\s*(.*?)END:VEVENT',value,re.S)
    if not blocks or len(blocks)>5000:raise ValueError('calendar layout')
    rows=[];seen=set()
    for block in blocks:
        fields={}
        for line in block.splitlines():
            if ':' not in line:continue
            key,text=line.split(':',1)
            if key in fields:raise ValueError('duplicate calendar field')
            fields[key]=text
        title=fields.get('SUMMARY');dates=[(k,v) for k,v in fields.items() if k.startswith('DTSTART')]
        if not title or len(dates)!=1:raise ValueError('calendar event identity')
        key,value=dates[0]
        if key=='DTSTART' and re.fullmatch(r'\d{8}T\d{6}Z',value):at=datetime.strptime(value,'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        elif key in {'DTSTART;TZID=US-Eastern','DTSTART;TZID=America/New_York'}:
            local=datetime.strptime(value,'%Y%m%dT%H%M%S')
            at=local.replace(tzinfo=EASTERN)
            if at.utcoffset()!=at.replace(fold=1).utcoffset() or at.astimezone(timezone.utc).astimezone(EASTERN).replace(tzinfo=None)!=local:
                raise ValueError('ambiguous/nonexistent calendar local time')
        else:raise ValueError('unverified calendar timezone')
        if at.astimezone(EASTERN).strftime('%Y-%m')!=month:continue
        row=event('bls-calendar',title.replace(r'\,',','),Z(at),captured_at)
        if row['event_id'] in seen:raise ValueError('duplicate calendar event')
        seen.add(row['event_id']);rows.append(row)
    return rows


def bea_calendar(raw,*,month,captured_at):
    value=html_document(raw)
    # The selected year is page content, not the requested month or machine clock.
    years=re.findall(r'<option[^>]*selected[^>]*value="(20\d{2})"',value,re.I)
    if not years:
        years=re.findall(r'<option[^>]*value="(20\d{2})"[^>]*selected',value,re.I)
    if not years:years=re.findall(r'<th\b[^>]*>\s*Year (20\d{2})\s*</th>',value,re.I)
    if len(set(years))!=1:raise ValueError('BEA selected calendar year missing')
    year=int(years[0]);rows=[];links=[]
    if str(year)!=month[:4]:raise ValueError('BEA page does not cover requested calendar year')
    for block in re.findall(r'<tr\b[^>]*>(.*?)</tr>',value,re.S|re.I):
        d=re.search(r'class="release-date">([^<]+)',block)
        title=re.search(r'<td[^>]*class="release-title[^>]*>(.*?)</td>',block,re.S)
        tm=re.search(r'<small[^>]*>(\d{1,2}:\d{2} [AP]M)</small>',block)
        if not d or not title or not tm:continue
        local=datetime.strptime(f'{d.group(1)} {year} {tm.group(1)}','%B %d %Y %I:%M %p').replace(tzinfo=EASTERN)
        title=plain(title.group(1));at=Z(local)
        if local.strftime('%Y-%m')==month:rows.append(event('bea-calendar',title,at,captured_at))
        href=re.search(r'href="(/news/20\d{2}/[a-z0-9-]+)"',block)
        if href and parse_utc(at)<=parse_utc(captured_at):
            kind='bea-pce' if title.startswith('Personal Income and Outlays,') else 'bea-gdp' if title.startswith(('GDP (','Gross Domestic Product,')) else None
            if kind:links.append({'source_id':kind,'url':'https://www.bea.gov'+href.group(1),'scheduled_at':at,'title':title})
    if not re.search(r'class="release-date"',value):raise ValueError('BEA schedule empty or changed')
    latest={}
    for row in sorted(links,key=lambda r:r['scheduled_at']):latest[row['source_id']]=row
    return rows,list(latest.values())


def metric(code,label,value,unit,basis):
    return {'indicator':code,'label':label,'value':value,'unit':unit,'basis':basis}


def signed_match(body,pattern):
    matches=list(re.finditer(pattern,body,re.I))
    if len(matches)!=1:return None
    m=matches[0];direction=m.group('direction').lower();value=Decimal(m.group('value').replace(',',''))
    if direction in {'fell','decreased','declined','dropped','edged down','moved down'}:value=-value
    return format(value,'f')


def bls_release(raw,*,source_id,url,captured_at):
    value=html_document(raw);blocks=re.findall(r'<pre\b[^>]*>(.*?)</pre>',value,re.S|re.I)
    kind={'bls-cpi':'CONSUMER PRICE INDEX','bls-ppi':'PRODUCER PRICE INDEXES','bls-jobs':'THE EMPLOYMENT SITUATION'}[source_id]
    bodies=[plain(b) for b in blocks if re.search(re.escape(kind)+r'\s*[-–—]\s*[A-Z]+ 20\d{2}',plain(b))]
    if len(bodies)!=1:raise ValueError('BLS release body ambiguous')
    body=bodies[0]
    header=re.search(re.escape(kind)+r'\s*[-–—]\s*([A-Z]+) (20\d{2})',body)
    if not header or header.group(1).lower() not in MONTHS:raise ValueError('release period/identity missing')
    reference=f'{header.group(2)}-{MONTHS[header.group(1).lower()]:02}'
    pub=re.search(r'(\d{1,2}):(\d{2})\s*([ap])\.m\.\s*\(ET\)\s+\w+,\s+([A-Za-z]+) (\d{1,2}), (20\d{2})',body[:600])
    if not pub:raise ValueError('release publication time missing')
    h,mi,amp,mon,day,year=pub.groups();hour=int(h)%12+(12 if amp=='p' else 0)
    published=Z(datetime(int(year),MONTHS[mon.lower()],int(day),hour,int(mi),tzinfo=EASTERN))
    if parse_utc(published)>parse_utc(captured_at):raise ValueError('embargoed release not yet public')
    content=body[header.end():];metrics=[]
    movement=r'(?P<direction>increased|rose|advanced|climbed|fell|decreased|declined|dropped|edged up|edged down|moved up|moved down)'
    number=r'(?P<value>\d+(?:\.\d+)?)'
    if source_id=='bls-cpi':
        v=signed_match(content[:1200],r'The Consumer Price Index for All Urban Consumers \(CPI-U\) '+movement+' '+number+' percent')
        if v is not None:metrics.append(metric('cpi_mom','CPI月率',v,'%','month_change_seasonally_adjusted'))
        v=signed_match(content[:1200],r'Over the last 12 months, the all items index '+movement+' '+number+' percent')
        if v is not None:metrics.append(metric('cpi_yoy','CPI年率',v,'%','year_change_not_seasonally_adjusted'))
        v=signed_match(content[:2300],r'The index for all items less food and energy '+movement+' '+number+' percent in '+header.group(1))
        if v is not None:metrics.append(metric('core_cpi_mom','核心CPI月率',v,'%','month_change_seasonally_adjusted'))
    elif source_id=='bls-ppi':
        v=signed_match(content[:1200],r'The Producer Price Index for final demand '+movement+' '+number+' percent')
        if v is not None:metrics.append(metric('ppi_mom','最终需求PPI月率',v,'%','month_change_seasonally_adjusted'))
        v=signed_match(content[:1200],r'the index for final demand '+movement+' '+number+' percent for the 12 months ended in '+header.group(1))
        if v is not None:metrics.append(metric('ppi_yoy','最终需求PPI年率',v,'%','year_change_not_seasonally_adjusted'))
    else:
        summary=content.split('This news release presents',1)[0]
        # Explicit signed parenthesis avoids treating "changed little" as zero.
        n=re.search(r'nonfarm payroll employment \(([+-][\d,]+)\)',summary,re.I)
        if n:metrics.append(metric('nonfarm_payroll_change','非农就业人数变化',str(int(n.group(1).replace(',',''))),'人','month_change_seasonally_adjusted'))
        else:
            v=signed_match(summary,r'(?:Total )?nonfarm payroll employment '+movement+r' by (?P<value>[\d,]+)')
            if v is not None:metrics.append(metric('nonfarm_payroll_change','非农就业人数变化',v,'人','month_change_seasonally_adjusted'))
        n=re.search(r'the unemployment rate (?:\(|(?:was|at|of)\s+)(\d+(?:\.\d+)?) percent',summary,re.I)
        if n:metrics.append(metric('unemployment_rate','失业率',n.group(1),'%','level_seasonally_adjusted'))
    return {'release_id':source_id+'-'+identity(reference,published),'title':kind,'reference_period':reference,
      'published_at':published,'captured_at':captured_at,'source_id':source_id,'url':url,'metrics':metrics,
      'state':'available' if metrics else 'partial','excerpt':content[:1400],
      'limitation':'官方公布值独立保存，非市场预期；未提取的指标不猜数，不覆盖日历或历史预测。'}


def bea_release(raw,*,source_id,url,captured_at,scheduled_at,title):
    value=html_document(raw)
    # Require official release heading and publication metadata; schedule alone
    # does not certify an accidentally linked future or unrelated report.
    headings=[h for h in re.findall(r'<h1\b[^>]*>(.*?)</h1>',value,re.S|re.I) if plain(h)!='News Release']
    if len(headings)!=1:raise ValueError('BEA heading ambiguous')
    heading=plain(headings[0])
    if (source_id=='bea-pce' and not heading.startswith('Personal Income and Outlays')) or (
        source_id=='bea-gdp' and not any(x in heading for x in ['Gross Domestic Product','GDP'])):raise ValueError('BEA report identity')
    dates=re.findall(r'<div\b[^>]*class="[^"]*field--name-field-release-date[^>]*>(.*?)</div>',value,re.S)
    if len(dates)!=1:raise ValueError('BEA independent release header missing')
    header=plain(dates[0])
    stamp=re.fullmatch(r'EMBARGOED UNTIL RELEASE AT (\d{1,2}):(\d{2}) ([ap])\.m\. (EDT|EST), \w+, ([A-Za-z]+) (\d{1,2}), (20\d{2})',header)
    if not stamp:raise ValueError('BEA release timestamp format')
    hh,mm,amp,zone,mon,day,year=stamp.groups()
    published=datetime(int(year),MONTHS[mon.lower()],int(day),int(hh)%12+(12 if amp=='p' else 0),int(mm),tzinfo=EASTERN)
    if published.tzname()!=zone or Z(published)!=scheduled_at:raise ValueError('BEA schedule and release timestamp conflict')
    # Page schema often supplies a date-only datePublished. It is retained as
    # date-only evidence, never converted to an invented intraday publication.
    date_meta=re.search(r'"datePublished"\s*:\s*"(20\d{2}-\d{2}-\d{2})',value)
    pubdate=date_meta.group(1) if date_meta else published.date().isoformat()
    if not pubdate:
        m=re.search(r'(?:Release Date|Published on|News Release)\s*:?.{0,100}?([A-Z][a-z]+ \d{1,2}, 20\d{2})',plain(value))
        if m:pubdate=datetime.strptime(m.group(1),'%B %d, %Y').date().isoformat()
    if pubdate and pubdate!=parse_utc(scheduled_at).astimezone(EASTERN).date().isoformat():raise ValueError('BEA schedule/report date conflict')
    if parse_utc(scheduled_at)>parse_utc(captured_at):raise ValueError('future BEA release')
    if plain(title).lower()!=heading.lower():
        # GDP headlines use Gross Domestic Product where the calendar uses GDP;
        # calendar link remains explicit, but never infer a numeric period.
        if source_id=='bea-pce':raise ValueError('BEA reference period conflict')
    start=value.find(headings[0]);article=value[start:]
    paragraphs=[plain(p) for p in re.findall(r'<p\b[^>]*>(.*?)</p>',article,re.S|re.I)]
    paragraphs=[p for p in paragraphs if any(x in p.lower() for x in ['pce','gross domestic product','price index','personal income'])]
    if not paragraphs:raise ValueError('BEA release narrative missing')
    data_text=' '.join(paragraphs[:8]);excerpt=data_text[:1800];metrics=[]
    if source_id=='bea-gdp':
        v=signed_match(excerpt,r'Real gross domestic product \(GDP\) (?P<direction>increased|decreased) at an annual rate of (?P<value>\d+(?:\.\d+)?) percent')
        if v is not None:metrics.append(metric('gdp_qoq_annualized','实际GDP季度年化增长率',v,'%','quarter_change_annualized'))
    else:
        movement=r'(?P<direction>increased|decreased|rose|fell) (?P<value>\d+(?:\.\d+)?) percent'
        for prefix,suffix,basis,label in [('From the preceding month,','mom','month_change','月率'),
                                        ('From the same month one year ago,','yoy','year_change','年率')]:
            blocks=[p for p in paragraphs if p.startswith(prefix)]
            if len(blocks)!=1:continue
            block=blocks[0]
            v=signed_match(block,re.escape(prefix)+r' the PCE price index for [A-Za-z]+ '+movement)
            if v is not None:metrics.append(metric('pce_'+suffix,'PCE物价指数'+label,v,'%',basis))
            v=signed_match(block,r'Excluding food and energy, the PCE price index '+movement)
            if v is not None:metrics.append(metric('core_pce_'+suffix,'核心PCE物价指数'+label,v,'%',basis))
    # Unsupported prose/table layouts stay as sourced context, not guessed values.
    period=re.search(r'([A-Za-z]+) (20\d{2})$',heading) if source_id=='bea-pce' else re.search(r'(\d)(?:st|nd|rd|th) Quarter (20\d{2})',heading)
    reference=(f'{period.group(2)}-{MONTHS[period.group(1).lower()]:02}' if source_id=='bea-pce' else f'{period.group(2)}-Q{period.group(1)}') if period else heading
    return {'release_id':source_id+'-'+identity(url,scheduled_at),'title':heading,'reference_period':reference,
      'published_at':scheduled_at if pubdate else None,'captured_at':captured_at,'source_id':source_id,'url':url,
      'metrics':metrics,'state':'available' if metrics else 'partial','excerpt':excerpt,
      'limitation':'数值只在明确提取时提供；发布时间缺少独立日期核对时为未知，不冒充准时公布或市场预期。'}


def feed_news(raw,*,source_id,captured_at):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('XML entities forbidden')
    root=ET.fromstring(raw);items=root.findall('./channel/item')
    if root.tag!='rss' or not items or len(items)>500:raise ValueError('RSS layout changed/empty')
    rows=[]
    for item in items:
        title=plain(item.findtext('title') or '');url=(item.findtext('link') or '').strip()
        if not title or not valid_article_url(source_id,url):continue
        try:
            at=parsedate_to_datetime(item.findtext('pubDate') or '')
            published=Z(at) if at.tzinfo is not None else None
        except (ValueError,TypeError,OverflowError):published=None
        summary=plain(item.findtext('description') or '')[:280]
        # Full raw feeds remain saved. Keyword selection is a potential-relevance
        # screen, never a claim that every selected story affects gold.
        if not source_id.startswith('fed-') and not NEWS_WORDS.search(title+' '+summary):continue
        rows.append({'news_id':'news-'+identity(url,published),'title':title[:400],'summary':summary,'url':url,
          'publisher':'美联储官方' if source_id.startswith('fed-') else 'BBC','published_at':published,
          'captured_at':captured_at,'source_id':source_id,'evidence_level':'feed_summary_only',
          'state':'time_unknown' if published is None else 'future_timestamp' if parse_utc(published)>parse_utc(captured_at) else
             'recent' if (parse_utc(captured_at)-parse_utc(published)).total_seconds()<=72*3600 else 'old',
          'relevance_basis':'央行官方信息' if source_id.startswith('fed-') else '标题或摘要关键词；影响方向待分析'})
    return rows


def daily_yields(raw,*,captured_at,url):
    value=html_document(raw);rows=[]
    for block in re.findall(r'<tr\b[^>]*>(.*?)</tr>',value,re.S|re.I):
        day=re.search(r'<time[^>]*>(\d{2}/\d{2}/20\d{2})</time>',block)
        if not day:continue
        date=datetime.strptime(day.group(1),'%m/%d/%Y').date().isoformat()
        if date>=parse_utc(captured_at).astimezone(EASTERN).date().isoformat():continue
        for years,symbol in [('2','US2Y'),('10','US10Y')]:
            cells=re.findall(r'<td\b[^>]*headers="view-field-bc-'+years+r'year-table-column"[^>]*>(.*?)</td>',block,re.S)
            if len(cells)!=1:continue
            number=plain(cells[0])
            if not re.fullmatch(r'-?\d+(?:\.\d+)?',number):continue
            rows.append({'symbol':symbol,'value':number,'unit':'%','observation_date':date,'captured_at':captured_at,
              'source_id':'treasury-daily','url':url,'real_time_verified':False,'limitation':'财政部每日曲线参考值，日期不是盘中报价时间，不与实时交易收益率混合。'})
    if not rows:raise ValueError('no dated Treasury reference rates')
    last=max(r['observation_date'] for r in rows)
    return [r for r in rows if r['observation_date']==last]
