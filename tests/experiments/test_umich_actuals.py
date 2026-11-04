"""SYNTHETIC source/clock/identity failures and full packet flow; real HTTP forbidden."""
import calendar
import json
import threading
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest

from tests.experiments.test_model_selection import case as case, forbid_real_http as forbid_real_http, gateway_for
from tests.experiments.test_fixed_forecast_candidate import candidate, facts
from xau_next.domains.analysis.economic_actuals import project_actuals
from xau_next.domains.analysis.public import (
    bind_shared_research_refs, build_economic_context, build_packet, frozen_answer_schema, outbound_packet,
)
from xau_next.platform.economic_calendar.umich import MAX_BYTES, URL, fetch_umich_raw, umich_document
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, validate_plan


def page(period="2026-10", kind="Final", sentence=None):
    month = calendar.month_name[int(period[5:])]
    sentence = sentence if sentence is not None else (
        "Year-ahead inflation expectations ticked up from 4.6% last month to 4.7% this month. "
        "Long-run inflation expectations were 3.5%.")
    return (f'<h1>{kind} Results for {month} {period[:4]}</h1><table id="front_table">'
        f'<tr><td></td><td>{month[:3]}</td><td>Sep</td></tr>'
        f'<tr><td></td><td>{period[:4]}</td><td>2025</td></tr>'
        '<tr><td>Index of Consumer Sentiment</td><td>48.0</td><td>53.6</td></tr></table>'
        f'<p>{sentence}</p><script>Year-ahead inflation expectations at 99%.</script>').encode()


def document(raw=None, **kwargs):
    return umich_document(raw if raw is not None else page(), captured_at="2026-10-23T14:05:00Z",
        reference_period="2026-10", release_kind="final", data_kind="SYNTHETIC", **kwargs)


def actual_case(case, *, shared=True):
    q, plan, auth = candidate(case)
    q["prompt_version"] = "manual-analysis/23"
    cutoff = parse_utc(q["market_cutoff"])
    z = lambda at: at.isoformat().replace("+00:00", "Z")
    period = cutoff.strftime("%Y-%m")
    value = q["context"]["economic_context"]
    value["events"] = [dict(value["events"][0], event_id="sentiment", title="Revised UoM Consumer Sentiment",
        scheduled_at=z(cutoff-timedelta(minutes=10)), actual=None, actual_available_at=None)]
    inflation = deepcopy(value["events"][0])
    inflation.update(event_id="inflation", title="Revised UoM Inflation Expectations", previous="4.6%", forecast=None)
    value["events"].append(inflation)
    value["actual_supplement"] = umich_document(page(period), captured_at=z(cutoff-timedelta(seconds=2)),
        reference_period=period, release_kind="final", data_kind="SYNTHETIC")
    if not shared:
        value.pop("shared_research")
        q["context"]["research_policy"] = {"mode": "shared_calendar"}
    q["context"] = bind_shared_research_refs(q, q["context"])
    q["packet_text"] = build_packet({k: v for k, v in q.items() if k not in {"context", "packet_text"}},
        q["context"], frozen_answer_schema(q))
    plan.update(prompt_version="manual-analysis/23", input_revision="economic-actuals/1",
        external_research={"mode": q["context"]["research_policy"]["mode"], "context_path": "synthetic.json",
                           "context_sha256": digest(q["context"]["economic_context"])})
    return q, plan, auth


def test_parse_current_first_column_and_one_year_not_previous_or_five_year():
    value = document()
    assert value["values"] == {"consumer_sentiment": "48.0", "inflation_expectations_1y": "4.7%"}
    assert value["original_publication_at"] is None
    assert value["availability_basis"] == "first_observed_at_capture_not_original_publication"


@pytest.mark.parametrize("sentence", [
    "Year-ahead inflation expectations fell to 4.7% from 5.1% last month.",
    "Year-ahead inflation expectations remained unchanged at 4.7%.",
    "One-year inflation expectations increased from 4.0% to 4.7% this month.",
])
def test_one_year_wording_uses_unambiguous_current_number(sentence):
    assert document(page(sentence=sentence))["values"]["inflation_expectations_1y"] == "4.7%"


@pytest.mark.parametrize("sentence", [
    "Long-run inflation expectations at 3.5%.",
    "Year-ahead inflation expectations last month were 4.6%.",
    "Year-ahead inflation expectations moved to 4.7% or to 5.0%.",
    "Year-ahead inflation expectations at 4.7%. Year-ahead inflation expectations at 5.0%.",
])
def test_missing_or_ambiguous_inflation_is_partial_not_invented(sentence):
    d = document(page(sentence=sentence))
    assert d["state"] == "partial" and d["values"]["inflation_expectations_1y"] is None


@pytest.mark.parametrize("mutation", ["old_month", "preliminary", "no_header", "duplicate_header", "duplicate_table",
    "wrong_column_month", "wrong_column_year", "missing_sentiment", "duplicate_sentiment", "bad_number", "negative", "huge_number", "bad_utf8", "oversize"])
def test_source_layout_and_identity_fail_closed(mutation):
    raw = page()
    if mutation == "old_month": raw = page("2026-09")
    if mutation == "preliminary": raw = page(kind="Preliminary")
    if mutation == "no_header": raw = raw.replace(b"Final Results", b"Other Results")
    if mutation == "duplicate_header": raw += b"<h1>Final Results for October 2026</h1>"
    if mutation == "duplicate_table": raw += raw
    if mutation == "wrong_column_month": raw = raw.replace(b"<td>Oct</td>", b"<td>Sep</td>")
    if mutation == "wrong_column_year": raw = raw.replace(b"<td>2026</td>", b"<td>2025</td>")
    if mutation == "missing_sentiment": raw = raw.replace(b"Index of Consumer Sentiment", b"Current Economic Conditions")
    if mutation == "duplicate_sentiment": raw = raw.replace(b"</table>", b"<tr><td>Index of Consumer Sentiment</td><td>49.0</td></tr></table>")
    if mutation == "bad_number": raw = raw.replace(b"48.0", b"N/A")
    if mutation == "negative": raw = raw.replace(b"48.0", b"-1.0")
    if mutation == "huge_number": raw = raw.replace(b"48.0", b"9999")
    if mutation == "bad_utf8": raw += b"\xff"
    if mutation == "oversize": raw = b"a"*(MAX_BYTES+1)
    with pytest.raises(DomainError): document(raw)


@pytest.mark.parametrize("status,content_type,body,success", [
    (200, "text/html", page(), True), (302, "text/html", b"", False),
    (500, "text/html", b"unavailable", False), (200, "application/json", b"{}", False),
    (200, "text/html", b"a"*(MAX_BYTES+1), False)], ids=["ok", "redirect", "http500", "wrong_type", "oversize"])
def test_one_bounded_get_no_redirect_retry_or_auth(status, content_type, body, success):
    sent = []
    def handler(request):
        sent.append(request)
        assert str(request.url) == URL and request.method == "GET"
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        return httpx.Response(status, headers={"Content-Type": content_type, "Location": "https://example.com/"}, content=body)
    kwargs = dict(transport=httpx.MockTransport(handler), clock=lambda: "2026-10-23T14:05:00Z")
    if success:
        raw, captured = fetch_umich_raw(**kwargs)
        assert raw == body and captured == "2026-10-23T14:05:00Z"
    else:
        with pytest.raises((DomainError, httpx.HTTPError)): fetch_umich_raw(**kwargs)
    assert len(sent) == 1


@pytest.mark.parametrize("mutation", ["future_capture", "fake_url", "fake_id", "wrong_kind", "fake_sha",
    "real_in_synthetic", "pretend_publication", "fake_availability", "five_year_only", "fake_state"])
def test_actual_document_rejects_forged_provenance(mutation):
    d = document()
    if mutation == "future_capture": d["source"]["captured_at"] = "2026-10-23T14:06:00Z"
    if mutation == "fake_url": d["source"]["url"] = "https://example.com/"
    if mutation == "fake_id": d["source"]["source_id"] = "calendar-test"
    if mutation == "wrong_kind": d["release_kind"] = "preliminary"
    if mutation == "fake_sha": d["source"]["content_sha256"] = "xyz"
    if mutation == "real_in_synthetic": d["data_kind"] = "REAL"
    if mutation == "pretend_publication": d["original_publication_at"] = "2026-10-23T14:00:00Z"
    if mutation == "fake_availability": d["availability_basis"] = "scheduled_time"
    if mutation == "five_year_only": d["values"] = {"inflation_expectations_5y": "3.5%"}
    if mutation == "fake_state": d["state"] = "partial"
    with pytest.raises(DomainError): project_actuals(d, cutoff="2026-10-23T14:05:00Z", data_kind="SYNTHETIC")


@pytest.mark.parametrize("shared", [True, False])
def test_full_packet_projects_actuals_time_and_separate_sources_without_mutating_input(case, shared):
    q, p, _ = actual_case(case, shared=shared)
    validate_plan(p)
    original = deepcopy(q)
    value = facts(outbound_packet(q["packet_text"]))
    econ = value["economic_context"]
    assert {e["event_id"]: e["actual"] for e in econ["events"]} == {"sentiment": "48.0", "inflation": "4.7%"}
    assert all(e["source_id"] == "calendar-test" and e["actual_source_id"].startswith("umich-") for e in econ["events"])
    assert all(e["actual_available_at"] == econ["actual_supplement"]["source"]["captured_at"] for e in econ["events"])
    assert all(e["state"] == "actual_provided" for e in value["forecast_review_input"]["event_value_states"])
    assert econ["actual_supplement"]["source"]["source_id"] in value["evidence_refs"]
    assert all(e["actual"] is None for e in q["context"]["economic_context"]["events"])
    assert q == original


@pytest.mark.parametrize("mutation", ["future_event", "wrong_month", "wrong_title", "non_usd", "partial_inflation"])
def test_unmatched_or_not_yet_released_keeps_actual_missing(case, mutation):
    q, _, _ = actual_case(case)
    context = q["context"]
    v = context["economic_context"]
    event = v["events"][0]
    if mutation == "future_event": event["scheduled_at"] = q["window_end"]
    if mutation == "wrong_month":
        event["scheduled_at"] = (parse_utc(event["scheduled_at"])-timedelta(days=35)).isoformat().replace("+00:00", "Z")
        v["sources"][0]["coverage_start"] = (parse_utc(event["scheduled_at"])-timedelta(days=1)).isoformat().replace("+00:00", "Z")
    if mutation == "wrong_title": event["title"] = "Other sentiment"
    if mutation == "non_usd": event["currency"] = "EUR"
    if mutation == "partial_inflation":
        d = v["actual_supplement"]
        d.update(state="partial", missing=["inflation_expectations_1y_not_unambiguously_parsed"])
        d["values"]["inflation_expectations_1y"] = None
    result = build_economic_context(q, context)
    expected_id = "inflation" if mutation == "partial_inflation" else "sentiment"
    assert expected_id not in {e["event_id"] for e in result["matched_event_actuals"]}


@pytest.mark.parametrize("mutation", ["duplicate_event", "conflicting_actual", "forged_actual", "forged_time", "fake_source"])
def test_ambiguous_or_conflicting_event_cannot_overwrite(case, mutation):
    q, _, _ = actual_case(case)
    v = q["context"]["economic_context"]
    e = v["events"][0]
    if mutation == "duplicate_event": v["events"].append(dict(e, event_id="other-id"))
    if mutation == "conflicting_actual": e["actual"] = "49.0"
    if mutation in {"forged_actual", "forged_time", "fake_source"}:
        e.update(actual="48.0", actual_source_id=v["actual_supplement"]["source"]["source_id"],
            actual_available_at=v["actual_supplement"]["source"]["captured_at"])
        if mutation == "forged_actual": e["actual"] = "49.0"
        if mutation == "forged_time": e["actual_available_at"] = e["scheduled_at"]
        if mutation == "fake_source": e["actual_source_id"] = "fake"
    with pytest.raises(DomainError): build_economic_context(q, q["context"])


def test_weekend_release_background_survives_projection_without_becoming_future_event(case):
    q, _, _ = actual_case(case)
    v = q["context"]["economic_context"]
    for e in v["events"]:
        e["scheduled_at"] = (parse_utc(e["scheduled_at"])-timedelta(hours=7)).isoformat().replace("+00:00", "Z")
    q["packet_text"] = build_packet({k:v for k,v in q.items() if k not in {"context", "packet_text"}}, q["context"], frozen_answer_schema(q))
    econ = facts(outbound_packet(q["packet_text"]))["economic_context"]
    assert econ["events"] == [] and len(econ["matched_event_actuals"]) == 2
    assert all(h["event_ids"] == [] for h in econ["horizons"])


def test_new_actuals_do_not_change_any_old_template(case):
    q, _, _ = actual_case(case)
    for prompt in ["14", "17", "18", "19", "20", "21", "22"]:
        q["prompt_version"] = "manual-analysis/"+prompt
        context = deepcopy(q["context"])
        context["evidence_refs"] = [r for r in context["evidence_refs"] if not r.startswith("umich-")]
        schema = frozen_answer_schema(q)
        request = {k:v for k,v in q.items() if k not in {"context", "packet_text"}}
        first = build_packet(request, context, schema)
        context["economic_context"].pop("actual_supplement")
        second = build_packet(request, context, schema)
        assert first == second and outbound_packet(first) == outbound_packet(second)


def test_four_mock_models_receive_same_actuals_and_no_live_search_or_retries(case):
    flow, oldroot, _, _, answer, credentials, calls, _ = case
    q, p, auth = actual_case(case)
    root = oldroot.parent/"actuals-SYNTHETIC"
    p = seal_plan(root, p)
    auth["plan_sha256"] = digest(p)
    freeze_sample(root, "sample-0001", q, clock=flow.clock)
    with calls.db.write() as tx:
        calls.docs.insert(tx, "experience_experiment_budget", p["experiment_id"], {"count":58,"reserved_cost":"0","cost_unknown":True})
    sent = []
    result = run_sample(root, "sample-0001", authorization=auth,
        gateway=gateway_for(answer,sent,barrier=threading.Barrier(4)), credentials=credentials, calls=calls, clock=flow.clock)
    assert len(sent) == 4 and all(r["state"] == "succeeded" for r in result.values()), result
    assert len({s["input"] for s in sent}) == 1
    assert all("tools" not in s and s["text"]["format"]["strict"] for s in sent)
    assert {e["event_id"]: e["actual"] for e in facts(sent[0]["input"])["economic_context"]["events"]}["sentiment"] == "48.0"


@pytest.mark.parametrize("variant", ["complete", "partial", "wrong_release"])
def test_cli_preserves_source_raw_failure_and_existing_output(tmp_path, monkeypatch, variant):
    from tooling.experiments.model_selection import main
    from xau_next.platform.economic_calendar.public import calendar_document
    from xau_next.platform.economic_calendar import umich
    # All transport is mocked. REAL below exercises the CLI's real-input gate,
    # not a claim that this manufactured test calendar is live evidence.
    rows = [{"title": title, "country": "USD", "date": "2026-10-23T10:00:00-04:00",
             "impact": "High", "forecast": "46.3", "previous": "46.3"} for title in
            ["Revised UoM Consumer Sentiment", "Revised UoM Inflation Expectations"]]
    context = calendar_document(json.dumps(rows).encode(), captured_at="2026-10-23T14:01:00Z")
    source = tmp_path/"calendar.json"
    source.write_text(json.dumps(context), encoding="utf-8")
    before = source.read_bytes()
    root = tmp_path/"new-capture"
    raw = page(kind="Preliminary") if variant == "wrong_release" else page(
        sentence="No one-year number here." if variant == "partial" else None)
    calls = []
    def fetch():
        calls.append(True)
        return raw, "2026-10-23T14:05:00Z"
    monkeypatch.setattr(umich, "fetch_umich_raw", fetch)
    args = ["actuals", "--calendar", str(source), "--reference-period", "2026-10", "--release-kind", "final", "--root", str(root)]
    assert main(args) == (2 if variant == "wrong_release" else 0)
    assert (root/"umich-source.raw.html").read_bytes() == raw and source.read_bytes() == before
    if variant == "wrong_release":
        assert (root/"FAILURE.json").exists() and not (root/"economic-context.json").exists()
    else:
        result = json.loads((root/"CAPTURE.json").read_text(encoding="utf-8"))
        assert len(result["matched_event_actuals"]) == (1 if variant == "partial" else 2)
        assert result["new_model_calls"] == result["paid_search_requests"] == result["new_mt5_calls"] == 0
    assert main(args) == 2 and len(calls) == 1


def test_projection_rejects_forged_actual_and_recomputes_background(case):
    q, _, _ = actual_case(case)
    from xau_next.domains.analysis.economic_context import project_economic_context
    e = facts(q["packet_text"])["economic_context"]
    e["matched_event_actuals"] = [{"actual":"9999", "injected":"PRIVATE_FAKE"}]
    clean = project_economic_context(e, prompt_version="manual-analysis/23")
    assert all(row["actual"] != "9999" for row in clean["matched_event_actuals"])
    e["events"][0]["actual"] = "9999"
    with pytest.raises(DomainError): project_economic_context(e, prompt_version="manual-analysis/23")
