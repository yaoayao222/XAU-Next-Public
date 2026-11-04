"""SYNTHETIC providers and isolated API, no external sockets, credentials or MT5."""
import json
from copy import deepcopy
from datetime import datetime,timezone
import httpx
import pytest

from tests.analysis.test_analysis_lab import flow as flow,add_bundle,prepare,facts,counts,research
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.platform.economic_calendar.official import bls_calendar,bea_calendar,bls_release,bea_release,daily_yields,feed_news
from xau_next.platform.economic_calendar.multi_source import fetch_multi_source_research,dollar_series
from xau_next.shared.research_sources import PUBLIC_SOURCES,TOPICS,treasury_url
from xau_next.domains.analysis.macro_research import project_macro
from xau_next.domains.analysis.public import outbound_packet
from xau_next.shared.errors import DomainError

AT='2026-10-12T01:59:59Z';CUT='2026-10-12T02:00:00Z';END='2026-10-12T05:00:00Z'


@pytest.fixture(autouse=True)
def current_answer_format(legacy_request_format,monkeypatch):
    # This directory's older analysis tests default to schema1.0; this feature
    # must exercise the actual current1.7 packet and API projection instead.
    monkeypatch.setattr('xau_next.domains.analysis.service.ANSWER_SCHEMA_VERSION','1.7.0')


def source(name):
    return {'source_id':name,'url':PUBLIC_SOURCES[name] if name in PUBLIC_SOURCES else treasury_url(2026),
     'captured_at':AT,'status':'available','content_sha256':'a'*64,'message':'SYNTHETIC'}


def ics(times=('20261011T223000','20261011T230000','20261012T013000'),zone='US-Eastern'):
    return ('BEGIN:VCALENDAR\n'+''.join('BEGIN:VEVENT\nDTSTART;TZID='+zone+':'+t+'\nSUMMARY:CPI event '+str(i)+'\nEND:VEVENT\n'
        for i,t in enumerate(times))+'END:VCALENDAR').encode()


def release(kind='bls-cpi',body=None):
    heading={'bls-cpi':'CONSUMER PRICE INDEX','bls-ppi':'PRODUCER PRICE INDEXES','bls-jobs':'THE EMPLOYMENT SITUATION'}[kind]
    if body is None:body=('The Consumer Price Index for All Urban Consumers (CPI-U) increased 0.0 percent on a seasonally adjusted basis in September. '
        'Over the last 12 months, the all items index increased 3.7 percent before seasonal adjustment. '
        'The index for all items less food and energy rose 0.3 percent in September.')
    return ('<pre>8:30 a.m. (ET) Friday, October 9, 2026\n'+heading+' - SEPTEMBER 2026\n'+body+'</pre><pre>Other details, not another release identity</pre>').encode()


def feed(*,future=False,old=False,unknown=False):
    stamp='bad date' if unknown else 'Mon, 12 Oct 2026 03:00:00 GMT' if future else 'Mon, 05 Oct 2026 00:00:00 GMT' if old else 'Mon, 12 Oct 2026 01:00:00 GMT'
    return ('<rss><channel><item><title>Federal Reserve and gold outlook</title><description>Rate and energy headline only</description>'
      '<link>https://www.bbc.com/news/articles/synthetic</link><pubDate>'+stamp+'</pubDate></item></channel></rss>').encode()


def multi():
    value=research()
    value.update(version='macro-research/2',official_releases=[],news=[],reference_rates=[],quote_series=[],
      search={'mode':'shared_public_sources','searched_at':AT,'topics':TOPICS,'native_model_search':False,'paid_search_requests':0,'note':'SYNTHETIC'})
    for name in ['bls-calendar','bls-cpi','bbc-world','treasury-daily']:value['sources'].append(source(name))
    value['events']=bls_calendar(ics(),month='2026-10',captured_at=AT)
    value['official_releases']=[bls_release(release(),source_id='bls-cpi',url=PUBLIC_SOURCES['bls-cpi'],captured_at=AT)]
    value['news']=feed_news(feed(),source_id='bbc-world',captured_at=AT)
    value['reference_rates']=[{'symbol':'US10Y','value':'4.0','unit':'%','observation_date':'2026-10-09','captured_at':AT,
      'source_id':'treasury-daily','url':treasury_url(2026),'real_time_verified':False,'limitation':'SYNTHETIC daily, not intraday'}]
    return value


def test_official_calendars_explicit_zone_and_dst():
    rows=bls_calendar(ics(),month='2026-10',captured_at=AT)
    assert rows[0]['scheduled_at']=='2026-10-12T02:30:00Z' and rows[0]['importance'] is None
    winter=bls_calendar(ics(('20260109T083000',)),month='2026-01',captured_at=AT)
    assert winter[0]['scheduled_at']=='2026-01-09T13:30:00Z'
    for dates,zone in [(('20260308T023000',),'US-Eastern'),(('20261101T013000',),'US-Eastern'),(('20261011T223000',),'Unknown')]:
        with pytest.raises(ValueError):bls_calendar(ics(dates,zone),month='2026-10',captured_at=AT)


def test_bls_values_keep_zero_units_and_actual_period():
    r=bls_release(release(),source_id='bls-cpi',url=PUBLIC_SOURCES['bls-cpi'],captured_at=AT)
    assert r['reference_period']=='2026-09' and r['published_at']=='2026-10-09T12:30:00Z'
    assert [(m['indicator'],m['value']) for m in r['metrics']]==[('cpi_mom','0.0'),('cpi_yoy','3.7'),('core_cpi_mom','0.3')]
    jobs=bls_release(release('bls-jobs','Both nonfarm payroll employment (+29,000) and the unemployment rate (4.2 percent) changed little in September.'),
      source_id='bls-jobs',url=PUBLIC_SOURCES['bls-jobs'],captured_at=AT)
    assert jobs['metrics'][0]['value']=='29000' and jobs['metrics'][0]['unit']=='人'
    ppi=bls_release(release('bls-ppi','The Producer Price Index for final demand decreased 0.2 percent in September.'),
      source_id='bls-ppi',url=PUBLIC_SOURCES['bls-ppi'],captured_at=AT)
    assert ppi['metrics'][0]['value']=='-0.2'


@pytest.mark.parametrize('change',['future','duplicate','period','script_only'])
def test_ambiguous_or_future_release_not_invented(change):
    raw=release()
    if change=='future':raw=raw.replace(b'October 9',b'October 30')
    if change=='duplicate':raw=raw+raw
    if change=='period':raw=raw.replace(b'SEPTEMBER 2026',b'UNKNOWN 2026')
    if change=='script_only':raw=b'<script>'+raw+b'</script>'
    with pytest.raises(ValueError):bls_release(raw,source_id='bls-cpi',url=PUBLIC_SOURCES['bls-cpi'],captured_at=AT)


def test_bea_schedule_does_not_fetch_future_actual():
    raw=b'<table><th>Year 2026</th><tr><td><div class="release-date">October 29</div><small>8:30 AM</small></td><td class="release-title">Personal Income and Outlays, September 2026</td><td><a href="/news/2026/personal-income-and-outlays-september-2026">View</a></td></tr></table>'
    rows,links=bea_calendar(raw,month='2026-10',captured_at=AT)
    assert rows[0]['scheduled_at']=='2026-10-29T12:30:00Z' and rows[0]['actual'] is None and links==[]
    with pytest.raises(ValueError):bea_calendar(raw.replace(b'Year 2026',b'Year unknown'),month='2026-10',captured_at=AT)


def test_bea_metric_basis_and_exact_calendar_header_match():
    raw=b'''<h1>News Release</h1><h1>Personal Income and Outlays, August 2026</h1>
      <div class="field--name-field-release-date">EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Wednesday, September 30, 2026</div>
      <p>From the preceding month, the PCE price index for August increased 0.3 percent. Excluding food and energy, the PCE price index increased 0.2 percent.</p>
      <p>From the same month one year ago, the PCE price index for August increased 3.4 percent. Excluding food and energy, the PCE price index increased 3.0 percent from one year ago.</p>'''
    kw={'source_id':'bea-pce','url':'https://www.bea.gov/news/2026/personal-income-and-outlays-august-2026',
      'captured_at':AT,'scheduled_at':'2026-09-30T12:30:00Z','title':'Personal Income and Outlays, August 2026'}
    r=bea_release(raw,**kw)
    assert r['reference_period']=='2026-08' and len(r['metrics'])==4
    assert {m['basis'] for m in r['metrics']}=={'month_change','year_change'}
    with pytest.raises(ValueError):bea_release(raw.replace(b'EDT',b'EST'),**kw)
    with pytest.raises(ValueError):bea_release(raw,**{**kw,'scheduled_at':'2026-09-30T13:30:00Z'})


def test_shared_packet_has_three_risk_windows_official_values_and_news(flow):
    bundle=add_bundle(flow,multi());before=counts(flow)
    response=prepare(flow,bundle=bundle['bundle_id']);assert response.status_code==201,response.text
    packet=facts(response.json()['packet_text']);macro=packet['macro_research']
    assert macro['version']=='macro-research/2' and [len(w['upcoming_event_ids']) for w in macro['window_risks']]==[1,2,2]
    assert macro['total_event_count']==3 and macro['excluded_event_count']==1
    assert macro['official_releases'][0]['metrics'][0]['value']=='0.0' and len(macro['news'])==1
    assert all(a['state']=='not_sent' for a in response.json()['arms']) and counts(flow)==before and not flow.sent
    assert response.json()['window_risks']==macro['window_risks']
    wire=facts(outbound_packet(response.json()['packet_text']))['macro_research']
    assert wire==macro
    viewed=flow.get('/v1/analysis/lab')['bundle']
    assert viewed['input_revision']=='macro-research/2' and len(viewed['official_releases'])==1


@pytest.mark.parametrize('field',['future_capture','future_release','metric_unit','metric_basis','news_url','news_capture','rate_today','rate_realtime','pretend_model_search'])
def test_multi_source_forgery_or_time_leak_is_rejected(field):
    v=multi()
    if field=='future_capture':v['sources'][-1]['captured_at']='2026-10-12T02:00:01Z'
    if field=='future_release':v['official_releases'][0]['published_at']='2026-10-12T03:00:00Z'
    if field=='metric_unit':v['official_releases'][0]['metrics'][0]['unit']='人'
    if field=='metric_basis':v['official_releases'][0]['metrics'][0]['basis']='year_change'
    if field=='news_url':v['news'][0]['url']='https://example.com/forged'
    if field=='news_capture':v['news'][0]['captured_at']='2026-10-12T02:00:01Z'
    if field=='rate_today':v['reference_rates'][0]['observation_date']='2026-10-11'
    if field=='rate_realtime':v['reference_rates'][0]['real_time_verified']=True
    if field=='pretend_model_search':v['search']['native_model_search']=True
    with pytest.raises(DomainError):project_macro(v,cutoff=CUT,window_end=END)


def test_old_future_and_unknown_news_never_enter_predictive_packet():
    v=multi();v['news']=[]
    for kw in [{'future':True},{'old':True},{'unknown':True}]:v['news'].extend(feed_news(feed(**kw),source_id='bbc-world',captured_at=AT))
    p=project_macro(v,cutoff=CUT,window_end=END)
    assert not p['news'] and p['excluded_news_count']==p['total_news_count']==3
    assert project_macro(p,cutoff=CUT,window_end=END)==p


def test_daily_rates_are_dated_reference_not_an_intraday_quote():
    raw=b'<table><tr><td><time>10/09/2026</time></td><td headers="view-field-bc-2year-table-column">4.69</td><td headers="view-field-bc-10year-table-column">5.00</td></tr></table>'
    rates=daily_yields(raw,captured_at=AT,url=treasury_url(2026))
    assert len(rates)==2 and rates[0]['observation_date']=='2026-10-09' and rates[0]['real_time_verified'] is False
    assert all('observed_at' not in r for r in rates)


def test_dollar_series_binds_timestamp_and_hash_without_claiming_realtime():
    stamp=int(datetime(2026,10,12,1,30,tzinfo=timezone.utc).timestamp())
    raw=json.dumps({'chart':{'result':[{'meta':{'symbol':'DX-Y.NYB','instrumentType':'INDEX'},'timestamp':[stamp,stamp+60],
      'indicators':{'quote':[{'close':[100.0,100.1]}]}}]}}).encode()
    s=source('bls-calendar');s.update(source_id='DXY')
    rows=dollar_series(raw,source=s);assert len(rows)==2 and rows[0]['real_time_verified'] is False
    v=multi();dxy=next(r for r in v['sources'] if r['source_id']=='DXY');dxy.update(status='available',content_sha256='a'*64)
    v['quote_series']=rows
    assert len(project_macro(v,cutoff=CUT,window_end=END)['quote_series'])==2
    v['quote_series'][0]['content_sha256']='b'*64
    with pytest.raises(DomainError):project_macro(v,cutoff=CUT,window_end=END)


def test_capture_keeps_other_sources_when_jin10_and_some_sources_fail():
    calls=[]
    def handler(request):
        assert request.method=='GET' and 'Authorization' not in request.headers and 'Cookie' not in request.headers
        calls.append(str(request.url));url=str(request.url)
        if url==PUBLIC_SOURCES['bls-calendar']:return httpx.Response(200,content=ics())
        if url==PUBLIC_SOURCES['bls-cpi']:return httpx.Response(200,content=release())
        if url==PUBLIC_SOURCES['bbc-world']:return httpx.Response(200,content=feed())
        return httpx.Response(503,content=b'SYNTHETIC unavailable')
    v,raw=fetch_multi_source_research(month='2026-10',transport=httpx.MockTransport(handler),clock=lambda:AT)
    assert len(calls)==len(set(calls))==17 and v['state']=='partial'
    assert len(v['official_releases'])==1 and len(v['events'])==3 and len(v['news'])==1
    assert v['search']['native_model_search'] is False and v['search']['paid_search_requests']==0
    assert all(s['status']=='unavailable' for s in v['sources'] if s['source_id'].startswith('jin10'))
