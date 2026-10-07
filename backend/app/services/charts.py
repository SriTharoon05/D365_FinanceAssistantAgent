"""Charts of returned ERP evidence and a constrained, anonymous QuickChart renderer."""

import asyncio
import hashlib
import json
import math
import re
import time
import zlib
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

import httpx

from app.core.errors import AppError

OPEN_KINDS = {"invoice", "open_transaction", "overdue_invoice", "credit", "customer_transaction", "payment"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _amount(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
        # ERP decimals are bounded; refuse pathological exponents/precision before arithmetic or rendering.
        if (
            not amount.is_finite()
            or len(amount.as_tuple().digits) > 128
            or abs(amount.as_tuple().exponent) > 128
        ):
            return None
        return amount
    except (InvalidOperation, ValueError):
        return None


def _sum(values):
    result = Decimal(0)
    for value in values:
        with localcontext() as context:
            context.prec = max(
                context.prec,
                max(result.adjusted(), value.adjusted())
                - min(result.as_tuple().exponent, value.as_tuple().exponent)
                + 2,
            )
            result += value
    return result


def _day(value):
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        result = value.isoformat()
    elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            result = date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    else:
        return None
    return None if result in {"0001-01-01", "1900-01-01"} else result


def _timestamp(value):
    try:
        result = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _identity(row, *, payment=False, anonymous=0):
    source = _text(row.get("source_entity"))
    explicit = next(
        (
            row[key]
            for key in ("source_key", "SourceKey", "transaction_id", "record_id")
            if row.get(key) is not None
        ),
        None,
    )
    if isinstance(explicit, (str, int)) and not isinstance(explicit, bool) and str(explicit).strip():
        return ("key", source, str(explicit))
    invoice, voucher = _text(row.get("invoice_number")), _text(row.get("voucher"))
    reference = _text(row.get("reference")) if payment else ""
    if not (invoice or voucher or reference):
        # Without a natural key, preserve potentially distinct transactions rather than collapsing them.
        return ("anonymous", anonymous)
    return (
        "reference",
        source,
        invoice,
        voucher,
        reference,
        _day(row.get("payment_date" if payment else "transaction_date")),
        _amount(
            row.get("amount") if payment and row.get("amount") is not None else row.get("original_amount")
        ),
    )


def _descriptor(group, category, entries):
    company, account, currency = group
    if category == "payments":
        days = defaultdict(list)
        for entry in entries:
            days[entry["day"]].append(entry["amount"])
        period = "date"
        if len(days) > 250:
            months = defaultdict(list)
            for day, amounts in days.items():
                months[day[:7] + "-01"].extend(amounts)
            days, period = months, "month"
        if len(days) > 250:
            years = defaultdict(list)
            for day, amounts in days.items():
                years[day[:4] + "-01-01"].extend(amounts)
            days, period = years, "year"
        points = [{"label": day, "amount": str(_sum(days[day]))} for day in sorted(days)]
        kind, title = "line", f"Payments by {period}"
    else:
        ordered = sorted(
            entries, key=lambda item: (-abs(item["amount"]), item["label"], repr(item["identity"]))
        )
        points = [{"label": item["label"], "amount": str(item["amount"])} for item in ordered[:12]]
        if len(ordered) > 12:
            points = points[:11] + [
                {
                    "label": f"Other ({len(ordered) - 11} records)",
                    "amount": str(_sum(item["amount"] for item in ordered[11:])),
                }
            ]
        kind, title = "bar", "Overdue open amounts" if category == "overdue" else "Open amounts"
    fingerprint = json.dumps([group, category, points], sort_keys=True, separators=(",", ":"))
    return {
        "id": f"{category}-{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}",
        "kind": kind,
        "title": f"{title}: {account} ({currency})",
        "company": company,
        "currency": currency,
        "description": (
            f"Snapshot of {len(entries)} plotted records returned for {company.upper()}/{account}. "
            + (
                f"Amounts are grouped by calendar {period}. "
                if category == "payments" and period != "date"
                else ""
            )
            + "Amounts are from those returned records; this is not a complete ledger."
        ),
        "points": points,
        "source_count": len(entries),
    }


def build_charts(records: list[dict]) -> list[dict]:
    """Derive signed, currency-separated charts only from financial evidence with provenance."""
    open_rows, payments = defaultdict(dict), defaultdict(dict)
    for index, row in enumerate(records):
        if not isinstance(row, dict) or row.get("kind") not in OPEN_KINDS:
            continue
        company, account, currency = (_text(row.get(key)) for key in ("company", "account", "currency"))
        retrieved = _timestamp(row.get("retrieved_at"))
        if not (
            company
            and account
            and re.fullmatch(r"[A-Za-z]{3}", currency)
            and _text(row.get("source_entity"))
            and retrieved
        ):
            continue
        group = (company.lower(), account, currency.upper())
        remaining = _amount(row.get("remaining_amount"))
        if remaining is not None:
            identity = _identity(row, anonymous=index)
            previous = open_rows[group].get(identity)
            overdue = row["kind"] == "overdue_invoice"
            label = _text(row.get("invoice_number")) or _text(row.get("voucher")) or f"Record {index + 1}"
            if previous is None or retrieved >= previous["retrieved"]:
                open_rows[group][identity] = {
                    "identity": identity,
                    "label": label,
                    "amount": remaining,
                    "retrieved": retrieved,
                    "overdue": overdue or bool(previous and previous["overdue"]),
                    "open_seen": not overdue or bool(previous and previous["open_seen"]),
                }
            elif overdue:
                previous["overdue"] = True
            else:
                previous["open_seen"] = True
        if row["kind"] != "payment":
            continue
        day = _day(row.get("payment_date"))
        paid = _amount(row.get("amount") if row.get("amount") is not None else row.get("original_amount"))
        if day is None or paid is None:
            continue
        identity = _identity(row, payment=True, anonymous=index)
        previous = payments[group].get(identity)
        if previous is None or retrieved >= previous["retrieved"]:
            payments[group][identity] = {
                "identity": identity,
                "amount": paid,
                "day": day,
                "retrieved": retrieved,
            }
    result = []
    for group in sorted(set(open_rows) | set(payments)):
        opened = list(open_rows[group].values())
        if opened:
            if any(row["open_seen"] for row in opened):
                result.append(_descriptor(group, "open", opened))
            overdue = [row for row in opened if row["overdue"]]
            if overdue:
                result.append(_descriptor(group, "overdue", overdue))
        if payments[group]:
            result.append(_descriptor(group, "payments", list(payments[group].values())))
    return result


def _png(data):
    if not data.startswith(PNG_SIGNATURE):
        return False
    offset, image_data = 8, False
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        end = offset + 12 + size
        if end > len(data):
            return False
        chunk = data[offset + 4 : offset + 8 + size]
        if zlib.crc32(chunk) & 0xFFFFFFFF != int.from_bytes(data[end - 4 : end], "big"):
            return False
        kind = chunk[:4]
        if offset == 8:
            if kind != b"IHDR" or size != 13:
                return False
            width, height = int.from_bytes(chunk[4:8], "big"), int.from_bytes(chunk[8:12], "big")
            if not (0 < width <= 4096 and 0 < height <= 4096):
                return False
        if kind == b"IDAT":
            image_data = True
        if kind == b"IEND":
            return size == 0 and end == len(data) and image_data
        offset = end
    return False


class QuickChartRenderer:
    """POST controlled Chart.js data to the free PNG endpoint; never publish public chart URLs."""

    def __init__(
        self,
        client=None,
        *,
        timeout_seconds=15,
        cache_ttl_seconds=300,
        cache_max_items=32,
        max_response_bytes=2 * 1024 * 1024,
    ):
        self.timeout_seconds = min(max(float(timeout_seconds), 0.01), 15)
        self.cache_ttl_seconds = max(float(cache_ttl_seconds), 0)
        self.cache_max_items = min(max(int(cache_max_items), 1), 64)
        self.max_response_bytes = min(max(int(max_response_bytes), 1), 2 * 1024 * 1024)
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False)
        self._cache = OrderedDict()

    def _payload(self, chart, theme):
        currency, kind, points = chart.get("currency"), chart.get("kind"), chart.get("points")
        if (
            theme not in {"light", "dark"}
            or kind not in {"bar", "line"}
            or not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or not isinstance(points, list)
            or not 1 <= len(points) <= 250
        ):
            raise AppError("CHART_DATA_INVALID", "The chart data could not be rendered.", status_code=422)
        labels, values = [], []
        for index, point in enumerate(points):
            amount = _amount(point.get("amount")) if isinstance(point, dict) else None
            if amount is None or not math.isfinite(float(amount)):
                raise AppError("CHART_DATA_INVALID", "The chart data could not be rendered.", status_code=422)
            label = _day(point.get("label")) if kind == "line" else f"Record {index + 1}"
            if label is None:
                raise AppError("CHART_DATA_INVALID", "The chart data could not be rendered.", status_code=422)
            if kind == "bar" and re.fullmatch(r"Other \(\d+ records\)", str(point.get("label", ""))):
                label = point["label"]
            labels.append(label)
            values.append(
                float(amount)
            )  # Exact Decimal values remain in the descriptor; floats are geometry only.
        foreground = "#f0eef8" if theme == "dark" else "#334155"
        background = "#beafff" if theme == "dark" else "#6152df"
        border = "#beafff" if theme == "dark" else "#5041ca"
        title = f"Returned open amounts ({currency})"
        if kind == "line":
            supplied_title = chart.get("title", "")
            period = next(
                (
                    name
                    for name in ("month", "year")
                    if str(supplied_title).startswith(f"Payments by {name}:")
                ),
                "date",
            )
            title = f"Returned payments by {period} ({currency})"
        grid = "rgba(240, 238, 248, 0.16)" if theme == "dark" else "rgba(51, 65, 85, 0.12)"
        compact = chart.get("render_size") == "compact"
        return {
            "version": "4",
            "format": "png",
            "width": 360 if compact else 720,
            "height": 280 if compact else 300,
            "devicePixelRatio": 1,
            "backgroundColor": "#252433" if theme == "dark" else "#ffffff",
            "chart": {
                "type": kind,
                "data": {
                    "labels": labels,
                    "datasets": [
                        {
                            "label": title,
                            "data": values,
                            "backgroundColor": background,
                            "borderColor": border,
                            "borderWidth": 2,
                            "pointRadius": 3,
                            "tension": 0,
                        }
                    ],
                },
                "options": {
                    "animation": False,
                    "plugins": {
                        "legend": {"display": False},
                        "title": {
                            "display": True,
                            "text": title,
                            "color": foreground,
                            "font": {"size": 16 if compact else 18},
                        },
                    },
                    "scales": {
                        "x": {"ticks": {"color": foreground, "font": {"size": 16}}, "grid": {"color": grid}},
                        "y": {
                            "beginAtZero": True,
                            "ticks": {"color": foreground, "font": {"size": 16}},
                            "grid": {"color": grid},
                        },
                    },
                },
            },
        }

    async def render(self, chart: dict, theme: str = "light") -> bytes:
        payload = self._payload(chart, theme)
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            self._cache.move_to_end(key)
            return cached[1]
        self._cache.pop(key, None)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self.client.stream(
                    "POST",
                    "https://quickchart.io/chart",
                    json=payload,
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200 or not response.headers.get(
                        "Content-Type", ""
                    ).lower().startswith("image/png"):
                        raise ValueError("Unusable chart response")
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) > self.max_response_bytes:
                        raise ValueError("Chart response exceeds limit")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise ValueError("Chart response exceeds limit")
                        body.extend(chunk)
                    image = bytes(body)
                    if not _png(image):
                        raise ValueError("Invalid PNG response")
        except (httpx.HTTPError, TimeoutError, ValueError, RuntimeError):
            raise AppError(
                "CHART_RENDER_UNAVAILABLE",
                "The chart image is temporarily unavailable. Try again shortly.",
                status_code=502,
                retryable=True,
            ) from None
        if self.cache_ttl_seconds:
            self._cache[key] = (time.monotonic() + self.cache_ttl_seconds, image)
            while len(self._cache) > self.cache_max_items:
                self._cache.popitem(last=False)
        return image

    async def close(self):
        self._cache.clear()
        if self._owns_client:
            await self.client.aclose()
