"""Fixed public research identities shared by ingestion and frozen validation."""
import re
from urllib.parse import urlsplit

PUBLIC_SOURCES = {
    'bls-calendar':'https://www.bls.gov/schedule/news_release/bls.ics',
    'bls-cpi':'https://www.bls.gov/news.release/cpi.nr0.htm',
    'bls-ppi':'https://www.bls.gov/news.release/ppi.nr0.htm',
    'bls-jobs':'https://www.bls.gov/news.release/empsit.nr0.htm',
    'bea-calendar':'https://www.bea.gov/news/schedule/full',
    'fed-policy':'https://www.federalreserve.gov/feeds/press_monetary.xml',
    'fed-speeches':'https://www.federalreserve.gov/feeds/speeches_and_testimony.xml',
    'bbc-world':'https://feeds.bbci.co.uk/news/world/rss.xml',
    'bbc-business':'https://feeds.bbci.co.uk/news/business/rss.xml',
}
TOPICS = ['黄金、美元与美债收益率','通胀、就业与央行政策','地缘冲突、关税与能源风险']
NEWS_WORDS = re.compile(r'\b(gold|bullion|inflation|dollar|treasur\w*|bond yields?|interest rates?|federal reserve|central bank\w*|'
    r'war|conflict|oil|energy|sanctions?|tariffs?|missiles?|iran|israel|russia|ukraine)\b', re.I)


def treasury_url(year):
    return ('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?'
            'type=daily_treasury_yield_curve&field_tdr_date_value='+str(year))


def valid_source_url(source_id,url):
    if source_id in PUBLIC_SOURCES:return url==PUBLIC_SOURCES[source_id]
    if source_id=='treasury-daily':return isinstance(url,str) and any(url==treasury_url(y) for y in range(2000,2100))
    if source_id in {'bea-gdp','bea-pce'}:
        return isinstance(url,str) and bool(re.fullmatch(r'https://www\.bea\.gov/news/20\d{2}/[a-z0-9-]+',url))
    return False


def valid_article_url(source_id,url):
    if not isinstance(url,str):return False
    u=urlsplit(url)
    if u.scheme!='https' or u.username or u.password or u.port not in {None,443}:return False
    if source_id.startswith('fed-'):
        return u.hostname=='www.federalreserve.gov' and u.path.startswith(('/newsevents/pressreleases/','/newsevents/speech/','/newsevents/testimony/'))
    if source_id.startswith('bbc-'):
        return u.hostname in {'www.bbc.com','www.bbc.co.uk','bbc.com','bbc.co.uk'} and u.path.startswith('/news/')
    return False
