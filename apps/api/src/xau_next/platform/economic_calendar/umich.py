"""Bounded public UoM read and conservative, version-aware extraction. No credentials."""
import hashlib
import re
from decimal import Decimal
from html.parser import HTMLParser

import httpx

from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc, utc_now

URL = "https://www.sca.isr.umich.edu/"
MAX_BYTES = 500_000
MONTHS = {name: i for i, name in enumerate(
    "January February March April May June July August September October November December".split(), 1)}


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.tables = 0
        self.in_table = False
        self.cell = None
        self.row = []
        self.rows = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "table" and dict(attrs).get("id") == "front_table":
            self.tables += 1
            self.in_table = True
        if self.in_table and tag == "tr":
            self.row = []
        if self.in_table and tag in {"td", "th"}:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if self.in_table and tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        if self.in_table and tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []
        if tag == "table":
            self.in_table = False

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)
            if self.in_table and self.cell is not None:
                self.cell.append(data)


def _invalid(code, message):
    raise DomainError(code, message, 409)


def fetch_umich_raw(*, transport=None, clock=utc_now):
    chunks = []
    with httpx.Client(transport=transport, timeout=20, follow_redirects=False) as client:
        with client.stream("GET", URL, headers={"Accept": "text/html"}) as response:
            response.raise_for_status()
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                _invalid("UMICH_CONTENT_TYPE_INVALID", "官方实际值来源未返回网页，未取用数值。")
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    _invalid("UMICH_TOO_LARGE", "官方网页超过大小上限，已停止且不重试。")
                chunks.append(chunk)
    captured = clock()
    parse_utc(captured)
    return b"".join(chunks), captured


def umich_document(raw, *, captured_at, reference_period, release_kind, data_kind="REAL"):
    if (not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", reference_period)
            or release_kind not in {"preliminary", "final"} or data_kind not in {"REAL", "SYNTHETIC"}):
        _invalid("UMICH_IDENTITY_INVALID", "必须明确资料月份和初值或终值。")
    parse_utc(captured_at)
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        _invalid("UMICH_TOO_LARGE", "官方网页缺失或超过大小上限。")
    page = _Page()
    try:
        page.feed(raw.decode("utf-8-sig", errors="strict"))
        page.close()
    except (UnicodeError, ValueError):
        _invalid("UMICH_FORMAT_INVALID", "官方网页无法解析，实际值保持缺失。")
    text = " ".join(" ".join(page.text).split())
    headings = re.findall(r"\b(Final|Preliminary) Results for ([A-Za-z]+) (20\d{2})\b", text)
    expected = [(release_kind.title(), next(k for k, v in MONTHS.items() if v == int(reference_period[5:])),
                 reference_period[:4])]
    if headings != expected:
        _invalid("UMICH_RELEASE_MISMATCH", "官网月份或初值/终值与本次要求不符，未补入旧值。")
    if page.tables != 1 or len(page.rows) < 3:
        _invalid("UMICH_FORMAT_INVALID", "官网结果表格结构变化，未猜测实际值。")
    month = expected[0][1][:3]
    if (len(page.rows[0]) < 2 or page.rows[0][1] != month
            or len(page.rows[1]) < 2 or page.rows[1][1] != reference_period[:4]):
        _invalid("UMICH_TABLE_PERIOD_INVALID", "官网表格第一列不是要求的月份，未读取其他月份。")
    matches = [row for row in page.rows if row and row[0] == "Index of Consumer Sentiment"]
    if len(matches) != 1 or len(matches[0]) < 2 or not re.fullmatch(r"\d+(?:\.\d+)?", matches[0][1]):
        _invalid("UMICH_FORMAT_INVALID", "官网消费者信心数值不唯一或格式变化。")
    sentiment = matches[0][1]
    if not Decimal("0") <= Decimal(sentiment) <= Decimal("200"):
        _invalid("UMICH_VALUE_INVALID", "官网消费者信心数值不在可接受范围内。")
    # Read the first sentence specifically about one-year expectations; never the
    # first percent (often last month) or the separate long-run/5-year reading.
    statements = list(re.finditer(r"\b(?:Year-ahead|One-year) inflation expectations\b", text, re.I))
    inflation = None
    if len(statements) == 1:
        sentence = re.split(r"\.(?=\s+[A-Z])", text[statements[0].end():statements[0].end()+400], maxsplit=1)[0]
        values = re.findall(r"\b(?:to|at)\s+(-?\d+(?:\.\d+)?)\s*%", sentence, re.I)
        if len(values) == 1 and Decimal("-20") <= Decimal(values[0]) <= Decimal("100"):
            inflation = values[0] + "%"
    return {"version": "umich-actuals/1", "data_kind": data_kind,
        "reference_period": reference_period, "release_kind": release_kind,
        "state": "available" if inflation is not None else "partial",
        "source": {"source_id": "umich-" + reference_period + "-" + release_kind,
            "publisher": "University of Michigan Surveys of Consumers", "url": URL,
            "captured_at": captured_at, "content_sha256": hashlib.sha256(raw).hexdigest()},
        "values": {"consumer_sentiment": sentiment, "inflation_expectations_1y": inflation},
        "missing": [] if inflation is not None else ["inflation_expectations_1y_not_unambiguously_parsed"],
        "availability_basis": "first_observed_at_capture_not_original_publication",
        "original_publication_at": None}
