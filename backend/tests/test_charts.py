"""Financial chart integrity and the bounded PNG transport."""

import asyncio
import json
import struct
import zlib
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from app.core.errors import AppError
from app.services.charts import QuickChartRenderer, build_charts


def record(**changes):
    return {
        "kind": "invoice",
        "company": "usmf",
        "account": "PRIVATE-CUSTOMER",
        "currency": "INR",
        "invoice_number": "PRIVATE-INVOICE",
        "voucher": "PRIVATE-VOUCHER",
        "original_amount": "120.00",
        "remaining_amount": "100.00",
        "transaction_date": "2026-09-20",
        "source_entity": "PrivateOpenEntities",
        "retrieved_at": "2026-10-07T00:00:00Z",
        **changes,
    }


def png():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )


def test_groups_are_separate_by_company_account_and_currency_with_stable_ids():
    rows = [record(), record(currency="EUR"), record(account="OTHER"), record(company="demf")]
    charts = build_charts(rows)
    assert len(charts) == 4
    assert len({chart["id"] for chart in charts}) == 4
    assert [chart["id"] for chart in charts] == [chart["id"] for chart in build_charts(list(reversed(rows)))]
    assert {chart["currency"] for chart in charts} == {"INR", "EUR"}
    assert all(chart["source_count"] == 1 and len(chart["points"]) == 1 for chart in charts)
    assert all(
        "complete ledger" in chart["description"] and "returned" in chart["description"] for chart in charts
    )
    assert any("OTHER" in chart["title"] for chart in charts)


def test_same_invoice_and_voucher_deduplicates_across_kinds_using_latest_amount():
    charts = build_charts(
        [
            record(),
            record(kind="overdue_invoice"),
            record(remaining_amount="99.99", retrieved_at="2026-10-07T00:01:00Z"),
        ]
    )
    assert len(charts) == 2
    assert all(chart["source_count"] == 1 for chart in charts)
    assert all(chart["points"] == [{"label": "PRIVATE-INVOICE", "amount": "99.99"}] for chart in charts)


def test_distinct_vouchers_and_explicit_source_keys_are_not_lost():
    charts = build_charts(
        [
            record(voucher="FIRST"),
            record(voucher="SECOND"),
            record(source_key=1),
            record(source_key=2),
            record(source_key=2, kind="overdue_invoice"),
        ]
    )
    assert charts[0]["source_count"] == 4
    assert len(charts[0]["points"]) == 4


def test_overdue_only_evidence_has_one_overdue_view_without_identical_open_view():
    charts = build_charts(
        [
            record(kind="overdue_invoice"),
            record(kind="overdue_invoice", invoice_number="ANOTHER", voucher="ANOTHER"),
        ]
    )
    assert len(charts) == 1
    assert charts[0]["id"].startswith("overdue-")
    assert charts[0]["source_count"] == 2


def test_overdue_and_signed_open_payment_keep_both_distinct_views():
    charts = build_charts(
        [
            record(kind="overdue_invoice"),
            record(kind="payment", invoice_number="", voucher="PAY", remaining_amount="-5.00"),
        ]
    )
    assert len(charts) == 2
    assert charts[0]["source_count"] == 2 and charts[1]["source_count"] == 1
    assert {point["amount"] for point in charts[0]["points"]} == {"100.00", "-5.00"}


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "-Infinity", "bad", True, {}, "1e100000"])
def test_missing_or_invalid_money_is_not_zero(value):
    assert build_charts([record(remaining_amount=value)]) == []


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "customer"},
        {"kind": "write_setup"},
        {"kind": "invoice_header"},
        {"account": None},
        {"currency": None},
        {"source_entity": None},
        {"retrieved_at": None},
        {"retrieved_at": "yesterday"},
        {"retrieved_at": "2026-10-07"},
    ],
)
def test_nonfinancial_or_unverified_records_do_not_become_charts(change):
    assert build_charts([record(**change)]) == []


def test_credits_and_open_payments_remain_signed_in_open_amounts():
    charts = build_charts(
        [
            record(remaining_amount="100.00"),
            record(kind="credit", invoice_number="CREDIT", remaining_amount="-15.25"),
            record(kind="payment", invoice_number="", voucher="PAYMENT", remaining_amount="-20.00"),
        ]
    )
    assert len(charts) == 1 and charts[0]["kind"] == "bar"
    assert {point["amount"] for point in charts[0]["points"]} == {"100.00", "-15.25", "-20.00"}


def test_more_than_twelve_records_are_aggregated_without_decimal_loss():
    rows = [
        record(invoice_number=f"INV-{i}", remaining_amount="99999999999999999999999999.99") for i in range(15)
    ]
    rows.append(record(invoice_number="CREDIT", remaining_amount="-0.01"))
    chart = build_charts(rows)[0]
    assert len(chart["points"]) == 12 and chart["source_count"] == 16
    assert chart["points"][-1]["label"] == "Other (5 records)"
    assert chart["points"][-1]["amount"] == "399999999999999999999999999.95"


def test_payment_dates_amounts_signs_and_repeated_records_are_verified():
    first = record(kind="payment", remaining_amount=None, payment_date="2026-09-20", amount="0.10")
    charts = build_charts(
        [
            first,
            first.copy(),
            record(
                kind="payment",
                voucher="PAY-2",
                remaining_amount=None,
                payment_date="2026-09-20",
                original_amount="0.20",
            ),
            record(
                kind="payment",
                voucher="REVERSAL",
                remaining_amount=None,
                payment_date="2026-09-21",
                amount="-0.05",
            ),
            record(kind="payment", remaining_amount=None, payment_date="invalid", amount="999999"),
            record(kind="payment", remaining_amount=None, payment_date="1900-01-01", amount="999999"),
        ]
    )
    assert len(charts) == 1
    assert charts[0]["source_count"] == 3
    assert charts[0]["points"] == [
        {"label": "2026-09-20", "amount": "0.30"},
        {"label": "2026-09-21", "amount": "-0.05"},
    ]


@pytest.mark.parametrize("period", ["month", "year"])
def test_long_payment_histories_are_aggregated_honestly_below_free_label_limit(period):
    days = (
        [date(2025, 1, 1) + timedelta(days=i) for i in range(251)]
        if period == "month"
        else [date(2000 + i // 12, i % 12 + 1, 20) for i in range(252)]
    )
    rows = [
        record(
            kind="payment",
            remaining_amount=None,
            voucher=f"PAY-{i}",
            payment_date=day.isoformat(),
            amount="0.10",
        )
        for i, day in enumerate(days)
    ]
    chart = build_charts(rows)[0]
    assert len(chart["points"]) <= 250
    assert f"by {period}" in chart["title"] and f"calendar {period}" in chart["description"]
    assert sum(Decimal(point["amount"]) for point in chart["points"]) == Decimal(len(rows)) / 10
    assert chart["source_count"] == len(rows)


async def test_renderer_uses_free_post_source_identifiers_controlled_configuration_and_theme():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    chart = build_charts([record(invoice_number="FTI-00000022")])[0]
    chart["chart"] = "malicious arbitrary javascript"
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        renderer = QuickChartRenderer(client)
        assert await renderer.render(chart, "light") == png()
        assert await renderer.render(chart, "light") == png()
        assert await renderer.render(chart, "dark") == png()
        await renderer.close()
    assert len(calls) == 2  # Successful cache entries are scoped to the full payload and theme.
    for request in calls:
        assert request.method == "POST" and str(request.url) == "https://quickchart.io/chart"
        assert "authorization" not in request.headers and "key" not in request.url.params
        body = request.content.decode()
        assert all(
            secret not in body
            for secret in ("PRIVATE-CUSTOMER", "PRIVATE-VOUCHER", "usmf", "PrivateOpenEntities", "malicious")
        )
        payload = json.loads(body)
        assert payload["version"] == "4" and payload["format"] == "png"
        assert payload["chart"]["data"]["labels"] == ["FTI-00000022"]
        assert payload["chart"]["data"]["datasets"][0]["data"] == [100.0]
        assert payload["chart"]["data"]["datasets"][0]["tension"] == 0
    assert json.loads(calls[0].content)["backgroundColor"] == "#ffffff"
    assert json.loads(calls[1].content)["backgroundColor"] == "#252433"


async def test_compact_image_has_only_controlled_size_and_its_own_cache_entry():
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    chart = build_charts([record()])[0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        renderer = QuickChartRenderer(client)
        await renderer.render(chart, "light")
        await renderer.render({**chart, "render_size": "compact", "width": 9999, "height": 9999}, "light")
        await renderer.render({**chart, "render_size": "compact"}, "light")
    assert len(calls) == 2
    assert (calls[0]["width"], calls[0]["height"]) == (720, 300)
    assert (calls[1]["width"], calls[1]["height"]) == (360, 280)
    assert calls[1]["chart"]["options"]["plugins"]["title"]["font"]["size"] == 16
    assert calls[0]["chart"]["options"]["plugins"]["title"]["font"]["size"] == 18
    assert calls[0]["chart"]["options"]["scales"]["x"]["ticks"]["font"]["size"] == 12
    assert calls[1]["chart"]["options"]["scales"]["y"]["ticks"]["font"]["size"] == 11


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("count", [2, 7, 12])
def test_invoice_identifiers_wrap_without_omitted_categories_on_desktop_and_mobile(compact, count):
    chart = build_charts(
        [
            record(invoice_number=f"FTI-{index:08}", remaining_amount=str(100 - index))
            for index in range(count)
        ]
    )[0]
    if compact:
        chart["render_size"] = "compact"
    payload = QuickChartRenderer()._payload(chart, "light")
    labels = payload["chart"]["data"]["labels"]
    assert len(labels) == count
    assert ["".join(label) if isinstance(label, list) else label for label in labels] == [
        point["label"] for point in chart["points"]
    ]
    assert all(not isinstance(label, list) or len(label) <= 3 for label in labels)
    ticks = payload["chart"]["options"]["scales"]["y" if compact else "x"]["ticks"]
    assert ticks["autoSkip"] is False and ticks["maxRotation"] == 0
    assert payload["chart"]["options"]["indexAxis"] == ("y" if compact else "x")
    if compact:
        assert labels == [point["label"] for point in chart["points"]]
        assert payload["height"] == max(280, count * 24 + 90)


def test_voucher_is_axis_category_when_invoice_identifier_is_unavailable():
    chart = build_charts([record(invoice_number="", voucher="ARPM000910")])[0]
    payload = QuickChartRenderer()._payload(chart, "light")
    assert payload["chart"]["data"]["labels"] == ["ARPM000910"]


@pytest.mark.parametrize("compact", [False, True])
def test_long_identifiers_are_bounded_distinct_and_full_identifiers_remain_in_data(compact):
    identifiers = ["FTI-" + "A" * 200 + suffix + "12345678" for suffix in ("FIRST", "SECOND")]
    chart = build_charts([record(invoice_number=identifier) for identifier in identifiers])[0]
    if compact:
        chart["render_size"] = "compact"
    payload = QuickChartRenderer()._payload(chart, "light")
    labels = payload["chart"]["data"]["labels"]
    assert labels[0] != labels[1]
    assert all(len(label) <= 3 for label in labels)
    assert all("…" in "".join(label) and len("".join(label)) <= 54 for label in labels)
    assert {point["label"] for point in chart["points"]} == set(identifiers)


@pytest.mark.parametrize("label", ["function(){return secret;}", "INV\nINJECT", "INV\u202e123", "X" * 2049])
async def test_unsafe_or_unbounded_identifiers_never_reach_quickchart(label):
    def transport(request):
        raise AssertionError("Unsafe category data must remain local")

    chart = build_charts([record(invoice_number=label)])[0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AppError) as error:
            await QuickChartRenderer(client).render(chart, "light")
    assert error.value.code == "CHART_DATA_INVALID"


def test_payment_date_axis_and_other_category_keep_their_meaning():
    payment = build_charts(
        [record(kind="payment", remaining_amount=None, payment_date="2026-10-05", amount="10")]
    )[0]
    renderer = QuickChartRenderer()
    assert renderer._payload(payment, "light")["chart"]["data"]["labels"] == ["2026-10-05"]
    open_chart = build_charts([record(invoice_number=f"INV-{index}") for index in range(15)])[0]
    payload = renderer._payload(open_chart, "light")
    last = payload["chart"]["data"]["labels"][-1]
    assert ("".join(last) if isinstance(last, list) else last).replace(" ", "") == "Other(4records)"


@pytest.mark.parametrize("compact", [False, True])
def test_numeric_axis_starts_at_zero_and_signed_amounts_survive_orientation_change(compact):
    chart = build_charts(
        [record(invoice_number="FTI-00000022"), record(invoice_number="CREDIT-1", remaining_amount="-15.25")]
    )[0]
    if compact:
        chart["render_size"] = "compact"
    payload = QuickChartRenderer()._payload(chart, "dark")
    assert payload["chart"]["data"]["datasets"][0]["data"] == [100.0, -15.25]
    scales = payload["chart"]["options"]["scales"]
    assert scales["x" if compact else "y"]["beginAtZero"] is True
    assert "beginAtZero" not in scales["y" if compact else "x"]


@pytest.mark.parametrize("failure", ["rate", "not_image", "corrupt", "network", "oversize"])
async def test_renderer_failures_are_generic_bounded_and_never_cached(failure):
    calls = []

    def transport(request):
        calls.append(request)
        if failure == "network":
            raise httpx.ReadTimeout("secret private upstream error", request=request)
        response = httpx.Response(
            429 if failure == "rate" else 200,
            content=(
                b"secret private body"
                if failure == "not_image"
                else png()[:-1]
                if failure == "corrupt"
                else png()
            ),
            headers={"Content-Type": "text/plain" if failure == "not_image" else "image/png"},
        )
        response.headers.pop("Content-Length", None)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        renderer = QuickChartRenderer(
            client, max_response_bytes=40 if failure == "oversize" else 2 * 1024 * 1024
        )
        for _ in range(2):
            with pytest.raises(AppError) as error:
                await renderer.render(build_charts([record()])[0], "light")
            assert error.value.code == "CHART_RENDER_UNAVAILABLE"
            assert "secret" not in error.value.message and "private" not in error.value.message
    assert len(calls) == 2


async def test_renderer_has_an_overall_deadline():
    async def transport(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        renderer = QuickChartRenderer(client, timeout_seconds=0.02)
        with pytest.raises(AppError, match="temporarily unavailable"):
            await renderer.render(build_charts([record()])[0], "light")


async def test_renderer_refuses_over_250_points_without_sending_or_dropping_data():
    rows = [
        record(
            kind="payment",
            remaining_amount=None,
            voucher=f"PAY-{i}",
            payment_date=f"{2000 + i}-06-20",
            amount="1",
        )
        for i in range(251)
    ]
    chart = build_charts(rows)[0]
    assert chart["source_count"] == 251 and len(chart["points"]) == 251

    def transport(request):
        raise AssertionError("An oversized chart must never reach the free renderer")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AppError) as error:
            await QuickChartRenderer(client).render(chart, "light")
        assert error.value.code == "CHART_DATA_INVALID"


async def test_success_cache_expires_and_close_preserves_injected_client():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        renderer = QuickChartRenderer(client, cache_ttl_seconds=0.01)
        chart = build_charts([record()])[0]
        await renderer.render(chart, "light")
        await asyncio.sleep(0.02)
        await renderer.render(chart, "light")
        await renderer.close()
        assert not client.is_closed
    assert len(calls) == 2
