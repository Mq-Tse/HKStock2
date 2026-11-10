from __future__ import annotations

import asyncio
import html
import io
import math
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader


class ProviderError(Exception):
    """公开数据源请求或解析失败。"""


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_RETRY_STATUSES = {408, 425, 500, 502, 503, 504}
_HK_TZ = timezone(timedelta(hours=8))
_MISSING = {"", "-", "--", "—", "N/A", "n/a", "None", "null"}
_CODE_RE = re.compile(r"^\d{5}$")
_DARK_RE = re.compile(r"暗[盘盤]")
_PLATFORM_RE = re.compile(r"富途|辉立|輝立")
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CLOSE_RE = re.compile(
    r"收(?:市|[盘盤]|[报報])?(?:[价價])?\s*"
    r"(?:为|為|于|於|在|[报報])?\s*"
    r"(?:HK\$|港元|港币|港幣)?\s*"
    rf"(?<![\d.\-])(?P<price>{_NUMBER})\s*(?:港元|港币|港幣|元)"
)
_CHANGE_CLOSE_RE = re.compile(
    r"(?:收)?(?:[涨漲]|跌)[^。；\n]{0,50}?[报報]\s*"
    r"(?:HK\$|港元|港币|港幣)?\s*"
    rf"(?<![\d.\-])(?P<price>{_NUMBER})\s*(?:港元|港币|港幣|元)"
)
_INTRADAY_RE = re.compile(r"一度|[盘盤][中裡]|暂[报報]|现[报報]|最新[报報]")


def _validate_code(code: str) -> str:
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        raise ProviderError(f"无效港股代码：{code!r}，应为5位数字字符串")
    return code


def _optional_date(value: Any, field: str) -> str | None:
    if value is None or str(value).strip() in _MISSING:
        return None
    text = str(value).strip()
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?", text
    ):
        raise ProviderError(f"{field}日期格式异常：{text!r}")
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ProviderError(f"{field}日期无效：{text!r}") from exc


def _required_date(value: Any, field: str) -> str:
    parsed = _optional_date(value, field)
    if parsed is None:
        raise ProviderError(f"{field}日期缺失")
    return parsed


def _optional_price(value: Any, field: str) -> float | None:
    if value is None or str(value).strip() in _MISSING:
        return None
    if isinstance(value, bool):
        raise ProviderError(f"{field}价格格式异常：{value!r}")
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"{field}价格格式异常：{value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ProviderError(f"{field}价格必须为正数：{value!r}")
    return number


def _f10_row(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProviderError("东方财富F10响应不是JSON对象")
    if payload.get("success") is not True or payload.get("code") != 0:
        raise ProviderError(
            "东方财富F10返回失败："
            f"code={payload.get('code')!r}, message={payload.get('message')!r}"
        )
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ProviderError("东方财富F10缺少result.data数组")
    if not result["data"]:
        raise ProviderError("东方财富F10未返回该股票发行资料")
    row = result["data"][0]
    if not isinstance(row, dict):
        raise ProviderError("东方财富F10发行资料记录格式异常")
    return row


def parse_issue_info(payload: Any) -> dict[str, float | str | None]:
    row = _f10_row(payload)
    raw_way = row.get("ISSUE_WAY")
    issue_way = (
        raw_way.strip()
        if isinstance(raw_way, str) and raw_way.strip() not in _MISSING
        else None
    )
    return {
        "issue_price": _optional_price(row.get("ISSUE_PRICE"), "F10 ISSUE_PRICE"),
        "listing_date": _optional_date(row.get("LISTING_DATE"), "F10 LISTING_DATE"),
        "issue_way": issue_way,
    }


def parse_price_history(payload: Any) -> dict[str, float | str]:
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        code = payload.get("rc") if isinstance(payload, dict) else None
        raise ProviderError(f"东方财富日K返回失败：rc={code!r}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
        raise ProviderError("东方财富日K缺少data.klines数组")
    if not data["klines"]:
        raise ProviderError("上市日期没有日K记录，无法取得首日收盘价")

    first_close = None
    first_trade_date = None
    highs: list[float] = []
    lows: list[float] = []
    for index, kline in enumerate(data["klines"]):
        if not isinstance(kline, str):
            raise ProviderError("东方财富日K记录不是CSV字符串")
        fields = kline.split(",")
        if len(fields) < 5:
            raise ProviderError(f"东方财富日K字段不足：{kline!r}")
        trade_date = _required_date(fields[0], "日K交易")
        high = _optional_price(fields[3], "日K最高")
        low = _optional_price(fields[4], "日K最低")
        if high is None or low is None:
            raise ProviderError("日K历史最高价或最低价缺失")
        if low > high:
            raise ProviderError(f"日K最低价高于最高价：{kline!r}")
        highs.append(high)
        lows.append(low)
        if index == 0:
            first_trade_date = trade_date
            first_close = _optional_price(fields[2], "日K首日收盘")
            if first_close is None:
                raise ProviderError("日K首日收盘价缺失")

    return {
        "first_close": first_close,
        "first_trade_date": first_trade_date,
        "historical_low": min(lows),
        "historical_high": max(highs),
    }


def _tencent_day_rows(payload: Any, code: str) -> list[list[str]]:
    code = _validate_code(code)
    if not isinstance(payload, dict) or payload.get("code") != 0:
        status = payload.get("code") if isinstance(payload, dict) else None
        raise ProviderError(f"腾讯日K返回失败：code={status!r}")
    data = payload.get("data")
    stock = data.get(f"hk{code}") if isinstance(data, dict) else None
    rows = stock.get("day") if isinstance(stock, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ProviderError("腾讯日K缺少上市以来的day数组")
    return rows


def parse_tencent_price_history(payload: Any, code: str) -> dict[str, float | str]:
    rows = _tencent_day_rows(payload, code)
    klines = []
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) < 5
            or not all(isinstance(field, str) for field in row[:5])
        ):
            raise ProviderError(f"腾讯日K记录格式异常：{row!r}")
        klines.append(",".join(row[:5]))
    return parse_price_history({"rc": 0, "data": {"klines": klines}})


def _optional_volume(value: Any, field: str) -> float:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"{field}成交量格式异常：{value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ProviderError(f"{field}成交量必须为非负数：{value!r}")
    return number


def parse_eastmoney_trends(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ProviderError("东方财富分时返回失败")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("trends"), list):
        raise ProviderError("东方财富分时缺少data.trends数组")
    if not data["trends"]:
        raise ProviderError("东方财富分时没有成交记录")

    points = []
    for row in data["trends"]:
        if not isinstance(row, str):
            continue
        fields = row.split(",")
        if len(fields) < 8:
            continue
        try:
            moment = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(
                tzinfo=_HK_TZ
            )
            price = _optional_price(fields[2], "分时价格")
            average = _optional_price(fields[7], "分时均价")
        except (ProviderError, ValueError):
            continue
        points.append(
            {
                "time": int(moment.timestamp()),
                "label": moment.strftime("%H:%M"),
                "price": price,
                "avg": average,
            }
        )
    if not points:
        raise ProviderError("东方财富分时没有有效记录")

    try:
        pre_close = _optional_price(data.get("preClose"), "分时昨收")
    except ProviderError:
        pre_close = None
    name = data.get("name")
    return {
        "items": points,
        "pre_close": pre_close,
        "name": name if isinstance(name, str) else None,
    }


def parse_eastmoney_klines(payload: Any, code: str) -> dict[str, Any]:
    code = _validate_code(code)
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ProviderError("东方财富K线返回失败")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
        raise ProviderError("东方财富K线缺少data.klines数组")
    if data.get("code") not in (None, code):
        raise ProviderError(
            f"东方财富K线代码不匹配：期望{code}，实际{data.get('code')!r}"
        )
    if not data["klines"]:
        raise ProviderError("东方财富K线没有成交记录")

    items = []
    for row in data["klines"]:
        if not isinstance(row, str):
            continue
        fields = row.split(",")
        if len(fields) < 6:
            continue
        try:
            trade_date = _required_date(fields[0], "K线交易")
            open_price = _optional_price(fields[1], "K线开盘")
            close_price = _optional_price(fields[2], "K线收盘")
            high = _optional_price(fields[3], "K线最高")
            low = _optional_price(fields[4], "K线最低")
            volume = _optional_volume(fields[5], "K线")
        except ProviderError:
            continue
        if low > high:
            continue
        items.append(
            {
                "time": trade_date,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": volume,
            }
        )
    if not items:
        raise ProviderError("东方财富K线没有有效记录")
    return {"items": items}


def parse_tencent_klines(payload: Any, code: str, period: str) -> dict[str, Any]:
    code = _validate_code(code)
    status = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or status != 0:
        raise ProviderError(f"腾讯K线返回失败：code={status!r}")
    data = payload.get("data")
    stock = data.get(f"hk{code}") if isinstance(data, dict) else None
    rows = stock.get(period) if isinstance(stock, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ProviderError(f"腾讯K线缺少{period}数组")

    items = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            trade_date = _required_date(row[0], "腾讯K线交易")
            open_price = _optional_price(row[1], "腾讯K线开盘")
            close_price = _optional_price(row[2], "腾讯K线收盘")
            high = _optional_price(row[3], "腾讯K线最高")
            low = _optional_price(row[4], "腾讯K线最低")
            volume = _optional_volume(row[5], "腾讯K线")
        except ProviderError:
            continue
        if low > high:
            continue
        items.append(
            {
                "time": trade_date,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": volume,
            }
        )
    if not items:
        raise ProviderError("腾讯K线没有有效记录")
    return {"items": items}


def parse_tencent_minute(payload: Any, code: str) -> dict[str, Any]:
    code = _validate_code(code)
    status = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or status != 0:
        raise ProviderError(f"腾讯分时返回失败：code={status!r}")
    data = payload.get("data")
    stock = data.get(f"hk{code}") if isinstance(data, dict) else None
    inner = stock.get("data") if isinstance(stock, dict) else None
    rows = inner.get("data") if isinstance(inner, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ProviderError("腾讯分时没有成交记录")

    raw_date = inner.get("date") if isinstance(inner, dict) else None
    try:
        trade_date = datetime.strptime(str(raw_date), "%Y%m%d").date()
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"腾讯分时日期异常：{raw_date!r}") from exc

    points = []
    for row in rows:
        if not isinstance(row, str):
            continue
        fields = row.split(" ")
        if len(fields) < 4 or not re.fullmatch(r"\d{3,4}", fields[0]):
            continue
        clock = fields[0].zfill(4)
        try:
            price = _optional_price(fields[1], "腾讯分时价格")
            volume = _optional_volume(fields[2], "腾讯分时")
            amount = _optional_volume(fields[3], "腾讯分时")
        except ProviderError:
            continue
        moment = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            int(clock[:2]),
            int(clock[2:]),
            tzinfo=_HK_TZ,
        )
        average = amount / volume if volume > 0 else None
        points.append(
            {
                "time": int(moment.timestamp()),
                "label": f"{clock[:2]}:{clock[2:]}",
                "price": price,
                "avg": round(average, 4) if average else None,
            }
        )
    if not points:
        raise ProviderError("腾讯分时没有有效记录")

    quote = stock.get("qt", {}).get(f"hk{code}") if isinstance(stock, dict) else None
    pre_close = None
    name = None
    if isinstance(quote, list) and len(quote) > 4:
        try:
            pre_close = _optional_price(quote[4], "腾讯昨收")
        except ProviderError:
            pre_close = None
        if isinstance(quote[1], str):
            name = quote[1]
    return {"items": points, "pre_close": pre_close, "name": name}


def parse_tencent_quotes(text: str, codes: list[str]) -> dict[str, dict]:
    normalized = list(dict.fromkeys(_validate_code(code) for code in codes))
    quotes = {
        code: {"latest_price": None, "quote_date": None, "quote_time": None}
        for code in normalized
    }
    if not isinstance(text, str):
        raise ProviderError("腾讯批量行情响应必须是字符串")
    for match in re.finditer(r'v_hk(\d{5})="([^"]*)";', text):
        code = match.group(1)
        if code not in quotes:
            continue
        fields = match.group(2).split("~")
        if len(fields) < 31 or fields[2] != code:
            raise ProviderError(f"腾讯批量行情记录格式异常：{code}")
        raw_price = fields[3].strip()
        price = (
            None
            if re.fullmatch(r"0+(?:\.0+)?", raw_price)
            else _optional_price(raw_price, f"股票{code}最新")
        )
        quote_date = None
        quote_time = None
        timestamp = fields[30].strip()
        if timestamp not in _MISSING:
            try:
                moment = datetime.strptime(timestamp, "%Y/%m/%d %H:%M:%S")
            except ValueError as exc:
                raise ProviderError(
                    f"股票{code}腾讯行情时间异常：{timestamp!r}"
                ) from exc
            quote_date = moment.date().isoformat()
            quote_time = moment.time().isoformat()
        quotes[code] = {
            "latest_price": price,
            "quote_date": quote_date,
            "quote_time": quote_time,
        }
    return quotes


def _extract_platform_close(segment: str) -> float | None:
    matches = [*_CLOSE_RE.finditer(segment), *_CHANGE_CLOSE_RE.finditer(segment)]
    for match in sorted(matches, key=lambda item: item.start()):
        prefix = segment[: match.start()]
        boundary = max(prefix.rfind("。"), prefix.rfind("；"), prefix.rfind("\n"))
        statement = prefix[boundary + 1 :] + match.group()
        if not _DARK_RE.search(prefix[boundary + 1 :]):
            continue
        if _INTRADAY_RE.search(statement):
            continue
        try:
            close = _optional_price(match.group("price"), "暗盘收盘")
        except ProviderError:
            continue
        if close is not None:
            return close
    return None


def parse_grey_article(html: str, source_url: str = "") -> dict[str, Any]:
    if not isinstance(html, str):
        raise ProviderError("暗盘新闻HTML必须是字符串")
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(id="spanContent") or soup.find("article") or soup
    for node in body.select("script, style, .jsStock, .jsSS, .newsRelatedHeadline"):
        node.decompose()
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
    markers = list(_PLATFORM_RE.finditer(text))
    segments: dict[str, list[str]] = {"富途": [], "辉立": []}
    for index, marker in enumerate(markers):
        platform = "富途" if marker.group() == "富途" else "辉立"
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        segments[platform].append(text[marker.start() : end])

    body_closes: dict[str, float] = {}
    for platform in ("富途", "辉立"):
        for segment in segments[platform]:
            close = _extract_platform_close(segment)
            if close is not None:
                body_closes[platform] = close
                break

    # 部分AASTOCKS稿件只在标题保留收盘价；实测标题对应正文首个平台段落。
    headline = soup.title.get_text(" ", strip=True) if soup.title else ""
    headline_close = _extract_platform_close(headline)
    headline_platform = None
    if markers:
        headline_platform = "富途" if markers[0].group() == "富途" else "辉立"

    # 同一文章可有两个平台，正文显式收盘价优先，始终保持富途优先于辉立。
    for platform in ("富途", "辉立"):
        if platform in body_closes:
            close = body_closes[platform]
        elif platform == headline_platform and headline_close is not None:
            close = headline_close
        else:
            continue
        return {
            "grey_close": close,
            "grey_platform": platform,
            "grey_source_url": source_url or None,
        }
    return {
        "grey_close": None,
        "grey_platform": None,
        "grey_source_url": source_url or None,
    }


_THIRD_PARTY_PLATFORM = "格隆汇快讯"


def parse_unlabeled_grey_close(
    text: str, source_url: str = ""
) -> dict[str, Any] | None:
    """接受未标注平台、但明确写出暗盘收盘价的快讯，归为格隆汇快讯口径。

    带平台字样的稿件仍只走富途/辉立口径，避免把盘中价或归属不明的数字算作收盘价。
    """
    if not isinstance(text, str) or _PLATFORM_RE.search(text):
        return None
    close = _extract_platform_close(text)
    if close is None:
        return None
    return {
        "grey_close": close,
        "grey_platform": _THIRD_PARTY_PLATFORM,
        "grey_source_url": source_url or None,
    }


class _AsyncProvider:
    def __init__(
        self,
        client: httpx.AsyncClient | None,
        source: str,
        referer: str,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._source = source
        self._timeout = timeout or _TIMEOUT
        self._headers = {**_DEFAULT_HEADERS, "Referer": referer}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        for attempt in range(3):
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout,
                )
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise ProviderError(
                        f"{self._source}请求失败（已重试3次）：{exc}"
                    ) from exc
            else:
                if response.status_code in _RETRY_STATUSES and attempt < 2:
                    pass
                elif response.status_code != 200:
                    raise ProviderError(
                        f"{self._source}请求失败：HTTP {response.status_code}，{response.url}"
                    )
                else:
                    return response
            await asyncio.sleep(0.25 * (2**attempt))
        raise ProviderError(f"{self._source}请求失败：重试后仍无有效响应")

    async def _get_json(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._get(url, params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"{self._source}响应不是有效JSON：{response.url}") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"{self._source}JSON顶层不是对象：{response.url}")
        return payload


def _parse_ipo_page(html: str, page: int) -> tuple[list[dict[str, Any]], int]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    columns: dict[str, int] = {}
    for candidate in soup.find_all("table"):
        header_row = candidate.find("tr")
        if header_row is None:
            continue
        headers = [
            cell.get_text(" ", strip=True)
            for cell in header_row.find_all(["th", "td"])
        ]
        positions = {title: headers.index(title) for title in headers}
        if {"股票代码", "股票名称", "上市日期"}.issubset(positions):
            table = candidate
            columns = positions
            break
    if table is None:
        raise ProviderError(f"东方财富IPO列表第{page}页缺少目标表格或表头")

    body = table.find("tbody") or table
    records: list[dict[str, Any]] = []
    for row_number, row in enumerate(body.find_all("tr", recursive=False), start=1):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        required_index = max(columns.values())
        if len(cells) <= required_index:
            raise ProviderError(
                f"东方财富IPO列表第{page}页第{row_number}行字段不足"
            )
        values = [cell.get_text(" ", strip=True) for cell in cells]
        code = values[columns["股票代码"]]
        name = values[columns["股票名称"]]
        if not _CODE_RE.fullmatch(code) or not name:
            raise ProviderError(
                f"东方财富IPO列表第{page}页第{row_number}行代码或名称异常"
            )
        records.append(
            {
                "code": code,
                "name": name,
                "listing_date": _optional_date(
                    values[columns["上市日期"]], f"IPO第{page}页上市"
                ),
            }
        )
    if not records:
        raise ProviderError(f"东方财富IPO列表第{page}页没有有效记录")

    last_page = page
    for anchor in soup.find_all("a", href=True):
        match = re.search(
            r"(?:^|/)ipolist_(\d+)\.html(?:$|\?)", anchor["href"], re.I
        )
        if match:
            last_page = max(last_page, int(match.group(1)))
    return records, last_page


class EastmoneyProvider(_AsyncProvider):
    IPO_URL = "https://hk.eastmoney.com/ipolist.html"
    F10_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    TRENDS_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
    QUOTES_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
    TENCENT_HFQ_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
    TENCENT_MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    KLINE_PERIODS = {"day": 101, "week": 102, "month": 103}

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(client, "东方财富", "https://quote.eastmoney.com/")

    async def fetch_ipo_list(self) -> list[dict]:
        response = await self._get(self.IPO_URL)
        records, last_page = _parse_ipo_page(response.text, 1)
        for page in range(2, last_page + 1):
            page_url = urljoin(self.IPO_URL, f"ipolist_{page}.html")
            response = await self._get(page_url)
            page_records, _ = _parse_ipo_page(response.text, page)
            records.extend(page_records)
        unique: dict[tuple[str, str | None], dict[str, Any]] = {}
        for record in records:
            unique.setdefault((record["code"], record["listing_date"]), record)
        return list(unique.values())

    async def fetch_issue_info(self, code: str) -> dict:
        code = _validate_code(code)
        payload = await self._get_json(
            self.F10_URL,
            {
                "reportName": "RPT_HSPE_ISSUEINFO",
                "columns": (
                    "SECUCODE,LISTING_DATE,ISSUE_PRICE,ISSUE_WAY,"
                    "STR_FXJCU,LOW_PRICE,HIGH_PRICE"
                ),
                "quoteColumns": "",
                "filter": f'(SECUCODE="{code}.HK")',
                "pageNumber": "",
                "pageSize": "",
                "sortTypes": 1,
                "sortColumns": "ISSUE_PROCESS",
                "source": "F10",
                "client": "PC",
            },
        )
        row = _f10_row(payload)
        if row.get("SECUCODE") != f"{code}.HK":
            raise ProviderError(
                f"东方财富F10证券代码不匹配：期望{code}.HK，实际{row.get('SECUCODE')!r}"
            )
        return parse_issue_info(payload)

    async def fetch_price_history(self, code: str, listing_date: str) -> dict:
        code = _validate_code(code)
        listing_date = _required_date(listing_date, "上市")
        try:
            payload = await self._get_json(
                self.KLINE_URL,
                {
                    "secid": f"116.{code}",
                    "klt": 101,
                    "fqt": 0,
                    "beg": listing_date.replace("-", ""),
                    "end": "20500101",
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                },
            )
        except ProviderError as primary_error:
            try:
                rows = await self._fetch_tencent_day_rows(code, listing_date)
                klines = []
                for row in rows:
                    if (
                        not isinstance(row, list)
                        or len(row) < 5
                        or not all(isinstance(field, str) for field in row[:5])
                    ):
                        raise ProviderError(f"腾讯日K记录格式异常：{row!r}")
                    klines.append(",".join(row[:5]))
                result = parse_price_history({"rc": 0, "data": {"klines": klines}})
            except ProviderError as fallback_error:
                raise ProviderError(
                    f"东方财富日K失败：{primary_error}；"
                    f"腾讯备用日K失败：{fallback_error}"
                ) from fallback_error
        else:
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and data.get("code") != code:
                raise ProviderError(
                    f"东方财富日K代码不匹配：期望{code}，实际{data.get('code')!r}"
                )
            result = parse_price_history(payload)
        if result["first_trade_date"] < listing_date:
            raise ProviderError(
                "日K交易日与上市日不匹配（首个交易日早于上市日）："
                f"{result['first_trade_date']} < {listing_date}"
            )
        return result

    async def _fetch_tencent_day_rows(
        self, code: str, listing_date: str
    ) -> list[list[str]]:
        rows: list[list[str]] = []
        end = "2050-01-01"
        for _ in range(8):
            payload = await self._get_json(
                self.TENCENT_KLINE_URL,
                {"param": f"hk{code},day,{listing_date},{end},2000"},
            )
            segment = _tencent_day_rows(payload, code)
            rows = segment + rows
            try:
                earliest = date.fromisoformat(segment[0][0])
            except (TypeError, ValueError) as exc:
                raise ProviderError(
                    f"腾讯日K日期异常：{segment[0][0]!r}"
                ) from exc
            if earliest <= date.fromisoformat(listing_date):
                return rows
            end = (earliest - timedelta(days=1)).isoformat()
        raise ProviderError("腾讯备用日K未能覆盖上市首日")

    async def _fetch_hfq_rows(
        self, code: str, start: str, end: str
    ) -> list[list[str]]:
        payload = await self._get_json(
            self.TENCENT_HFQ_KLINE_URL,
            {"param": f"hk{code},day,{start},{end},640,hfq"},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        node = data.get(f"hk{code}") if isinstance(data, dict) else None
        if not isinstance(node, dict):
            raise ProviderError(f"腾讯后复权日K缺少 hk{code} 节点")
        rows = node.get("hfqday") or node.get("day") or []
        return rows

    @staticmethod
    def _first_bar_on_or_after(
        bars: list[list[str]], listing_date: str
    ) -> list[str] | None:
        """取上市日及以后的第一根日K。

        腾讯后复权端点会在上市前的区间补一根 2006-01-03 的填充根
        （与真实价格无关），必须按上市日过滤，否则会被误判为旧实体数据。
        """
        for bar in bars:
            if bar and len(bar) >= 3 and bar[0] >= listing_date:
                return bar
        return None

    async def fetch_hfq_anchors(
        self, code: str, listing_date: str, today: str
    ) -> dict[str, float | str]:
        """取后复权日K的首日与最新收盘价，用于剥离送转/拆股对至今收益的影响。"""
        code = _validate_code(code)
        listing_date = _required_date(listing_date, "上市")
        listing = date.fromisoformat(listing_date)
        bars = await self._fetch_hfq_rows(code, listing_date, today)
        if not bars:
            raise ProviderError("腾讯后复权日K无数据")
        last = bars[-1]
        first = self._first_bar_on_or_after(bars, listing_date)
        if first is None or (date.fromisoformat(first[0]) - listing).days > 10:
            # 单次请求最多 640 根，老股的首根落在窗口外，需补取上市初期。
            early_end = f"{listing.year + 2}{listing_date[4:]}"
            early = await self._fetch_hfq_rows(code, listing_date, early_end)
            first = self._first_bar_on_or_after(early, listing_date)
            if first is None:
                raise ProviderError("腾讯后复权日K缺少上市初期数据")
            if (date.fromisoformat(first[0]) - listing).days > 10:
                raise ProviderError(
                    f"后复权首日与上市日不匹配：{first[0]} vs {listing_date}"
                )
        return {
            "hfq_first_close": float(first[2]),
            "hfq_first_date": first[0],
            "hfq_last_close": float(last[2]),
            "hfq_last_date": last[0],
        }

    async def fetch_kline(self, code: str, period: str = "day") -> dict:
        code = _validate_code(code)
        if period not in {"intraday", "day", "week", "month"}:
            raise ProviderError(f"不支持的K线周期：{period!r}")

        if period == "intraday":
            try:
                payload = await self._get_json(
                    self.TRENDS_URL,
                    {
                        "secid": f"116.{code}",
                        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                        "ndays": 1,
                        "iscr": 0,
                    },
                )
            except ProviderError as primary_error:
                try:
                    payload = await self._get_json(
                        self.TENCENT_MINUTE_URL, {"code": f"hk{code}"}
                    )
                except ProviderError as fallback_error:
                    raise ProviderError(
                        f"东方财富分时失败：{primary_error}；"
                        f"腾讯备用分时失败：{fallback_error}"
                    ) from fallback_error
                result = parse_tencent_minute(payload, code)
                source = "tencent"
            else:
                result = parse_eastmoney_trends(payload)
                source = "eastmoney"
            return {"code": code, "period": period, "source": source, **result}

        klt = self.KLINE_PERIODS[period]
        try:
            payload = await self._get_json(
                self.KLINE_URL,
                {
                    "secid": f"116.{code}",
                    "klt": klt,
                    "fqt": 0,
                    "end": "20500101",
                    "lmt": 1200,
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": (
                        "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                    ),
                },
            )
        except ProviderError as primary_error:
            try:
                payload = await self._get_json(
                    self.TENCENT_KLINE_URL,
                    {"param": f"hk{code},{period},,2050-01-01,1200"},
                )
            except ProviderError as fallback_error:
                raise ProviderError(
                    f"东方财富K线失败：{primary_error}；"
                    f"腾讯备用K线失败：{fallback_error}"
                ) from fallback_error
            result = parse_tencent_klines(payload, code, period)
            source = "tencent"
        else:
            result = parse_eastmoney_klines(payload, code)
            source = "eastmoney"
        return {
            "code": code,
            "period": period,
            "source": source,
            "pre_close": None,
            "name": None,
            **result,
        }

    async def fetch_latest_quotes(self, codes: list[str]) -> dict[str, dict]:
        normalized = list(dict.fromkeys(_validate_code(code) for code in codes))
        quotes = {
            code: {"latest_price": None, "quote_date": None, "quote_time": None}
            for code in normalized
        }
        for start in range(0, len(normalized), 50):
            batch = normalized[start : start + 50]
            try:
                payload = await self._get_json(
                    self.QUOTES_URL,
                    {
                        "fltt": 2,
                        "invt": 2,
                        "fields": "f2,f12,f14,f124",
                        "secids": ",".join(f"116.{code}" for code in batch),
                    },
                )
            except ProviderError as primary_error:
                try:
                    response = await self._get(
                        "https://qt.gtimg.cn/q="
                        + ",".join(f"hk{code}" for code in batch)
                    )
                    quotes.update(
                        parse_tencent_quotes(
                            response.content.decode("gbk"), batch
                        )
                    )
                except (ProviderError, UnicodeDecodeError) as fallback_error:
                    raise ProviderError(
                        f"东方财富批量行情失败：{primary_error}；"
                        f"腾讯备用行情失败：{fallback_error}"
                    ) from fallback_error
                continue
            if payload.get("rc") != 0:
                raise ProviderError(f"东方财富批量行情返回失败：rc={payload.get('rc')!r}")
            data = payload.get("data")
            if data is None:
                continue
            if not isinstance(data, dict) or not isinstance(data.get("diff"), list):
                raise ProviderError("东方财富批量行情缺少data.diff数组")
            for item in data["diff"]:
                if not isinstance(item, dict):
                    raise ProviderError("东方财富批量行情记录格式异常")
                code = item.get("f12")
                if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
                    raise ProviderError(f"东方财富批量行情代码异常：{code!r}")
                if code not in quotes:
                    continue
                timestamp = item.get("f124")
                quote_date = None
                quote_time = None
                if timestamp is not None and str(timestamp).strip() not in _MISSING:
                    if isinstance(timestamp, bool):
                        raise ProviderError(f"股票{code}行情时间戳异常：{timestamp!r}")
                    try:
                        epoch = int(timestamp)
                    except (TypeError, ValueError) as exc:
                        raise ProviderError(
                            f"股票{code}行情时间戳异常：{timestamp!r}"
                        ) from exc
                    if epoch > 0:
                        moment = datetime.fromtimestamp(epoch, tz=_HK_TZ)
                        quote_date = moment.date().isoformat()
                        quote_time = moment.time().isoformat()
                quotes[code] = {
                    "latest_price": _optional_price(
                        item.get("f2"), f"股票{code}最新"
                    ),
                    "quote_date": quote_date,
                    "quote_time": quote_time,
                }
        return quotes


def _result_date(anchor: Any) -> date | None:
    container = anchor
    for _ in range(3):
        if container.parent is None:
            break
        container = container.parent
    match = re.search(r"ConvertToLocalTime\(\{dt:'(\d{4})/(\d{2})/(\d{2})", str(container))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _normalized_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[‐‑‒–—―]", "-", normalized)
    return re.sub(r"\s+", "", normalized)


# AASTOCKS稿件标题常把数字排成全角（如"健康１６０"），而其关键字检索是字面匹配，
# 只用半角数字检索会漏掉这类暗盘报道，需要同时生成全角变体。
_FULLWIDTH_DIGIT_TRANS = str.maketrans("0123456789", "０１２３４５６７８９")


def _grey_search_terms(name: str) -> list[str]:
    """从完整证券简称逐步生成更宽、但仍可由股票代码核验的检索词。"""
    original = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip()
    terms: list[str] = []

    def add(term: str) -> None:
        term = term.strip(" -\t")
        if len(term) < 2:
            return
        if term not in terms:
            terms.append(term)
        fullwidth = term.translate(_FULLWIDTH_DIGIT_TRANS)
        if len(fullwidth) >= 2 and fullwidth not in terms:
            terms.append(fullwidth)

    add(original)
    base = re.sub(r"\s*\((?:新|五百)\)$", "", original, flags=re.I)
    base = re.sub(r"\s*-\s*(?:DRS|B|W|P)$", "", base, flags=re.I)
    add(base)

    core = base
    for suffix in ("集团", "科技"):
        if core.endswith(suffix):
            core = core[: -len(suffix)]
            add(core)

    english = re.sub(
        r"\s+(?:INTL|HLDGS|HOLDINGS)$", "", base, flags=re.I
    ).strip()
    add(english)
    if re.fullmatch(r"[A-Za-z0-9 .&-]+", english) and " " in english:
        add(english.split()[0])
    return terms


class AAStocksProvider(_AsyncProvider):
    SEARCH_URL = "https://www.aastocks.com/sc/stocks/news/aafn/ipo-news/0"
    # 实测该检索页响应接近20秒，默认15秒超时会直接漏掉已有暗盘报道。
    SEARCH_TIMEOUT = httpx.Timeout(45.0, connect=10.0)
    # 兼容桌面旧链接、移动端 newscontent/news.aspx 及个股新闻页 stock-aafn-con 链接。
    _ARTICLE_HREF_RE = re.compile(
        r"/news/aafn-con/|/newscontent/[A-Za-z]+/|[?&]newsid=|/stock-aafn-con/",
        re.I,
    )

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            client,
            "AAStocks",
            "https://www.aastocks.com/",
            timeout=self.SEARCH_TIMEOUT,
        )

    @staticmethod
    def _empty(
        source_url: str | None = None, error: str | None = None
    ) -> dict[str, Any]:
        return {
            "grey_close": None,
            "grey_platform": None,
            "grey_source_url": source_url,
            "grey_status": "not_found",
            "grey_error": error,
        }

    async def fetch_grey_market(
        self, code: str, name: str, listing_date: str, issue_price: float | None = None
    ) -> dict:
        try:
            code = _validate_code(code)
            listing = date.fromisoformat(_required_date(listing_date, "上市"))
            name = name.strip() if isinstance(name, str) else ""
            if not name:
                raise ProviderError("AAStocks暗盘检索缺少股票名称")

            search_terms = _grey_search_terms(name)
            aliases = [_normalized_match_text(term) for term in search_terms]
            seen_urls: set[str] = set()
            last_url = None
            article_attempts = 0

            # 完整名称优先，只有当前检索词找不到有效收盘价时才尝试简称。
            for term in search_terms:
                response = await self._get(self.SEARCH_URL, {"key": term})
                soup = BeautifulSoup(response.text, "html.parser")
                close_candidates: list[str] = []
                other_candidates: list[str] = []
                for anchor in soup.find_all("a", href=True):
                    href = anchor["href"]
                    title = anchor.get_text(" ", strip=True)
                    if not self._ARTICLE_HREF_RE.search(href):
                        continue
                    if not _DARK_RE.search(title):
                        continue
                    normalized_title = _normalized_match_text(title)
                    if code not in normalized_title and not any(
                        alias in normalized_title for alias in aliases
                    ):
                        continue
                    published = _result_date(anchor)
                    if published and not listing - timedelta(days=45) <= published <= listing:
                        continue
                    is_close_title = "收" in title
                    # 非收盘标题只作候补，必须有日期以排除旧盘中报道。
                    if not is_close_title and published is None:
                        continue
                    url = urljoin(self.SEARCH_URL, href)
                    target = close_candidates if is_close_title else other_candidates
                    if url not in target:
                        target.append(url)

                parsed_results: list[dict[str, Any]] = []
                for url in close_candidates + other_candidates:
                    if url in seen_urls or article_attempts >= 8:
                        continue
                    seen_urls.add(url)
                    article_attempts += 1
                    response = await self._get(url)
                    article = BeautifulSoup(response.text, "html.parser")
                    article_text = article.get_text(" ", strip=True)
                    normalized_article = _normalized_match_text(article_text)
                    if code not in normalized_article and not any(
                        alias in normalized_article for alias in aliases
                    ):
                        continue
                    mentioned_codes = set(
                        re.findall(r"(?<!\d)(\d{5})\.HK", article_text)
                    )
                    if mentioned_codes and code not in mentioned_codes:
                        continue
                    last_url = str(response.url)
                    parsed = parse_grey_article(response.text, last_url)
                    if parsed["grey_close"] is not None:
                        parsed_results.append(parsed)

                # 跨候选文章也保持富途优先，而非返回首篇辉立结果。
                for platform in ("富途", "辉立"):
                    for parsed in parsed_results:
                        if parsed["grey_platform"] == platform:
                            return {
                                **parsed,
                                "grey_status": "ok",
                                "grey_error": None,
                            }

            if last_url:
                return self._empty(last_url, "暗盘新闻未包含可核验的富途或辉立收盘价")
            return self._empty()
        except ProviderError as exc:
            return self._empty(error=str(exc))


def _parse_listing_date(listing_date: str | None) -> date | None:
    if not listing_date:
        return None
    try:
        return date.fromisoformat(str(listing_date)[:10])
    except ValueError:
        return None


def _check_announcement_date(
    announced_date: date, expected_date: date, listing: date | None
) -> None:
    if announced_date == expected_date:
        return
    # 容错模板年份笔误（月日一致、年份不同）。
    if (
        announced_date.month == expected_date.month
        and announced_date.day == expected_date.day
    ):
        return
    # 容错源站日期笔误：公告日落在文件名日期前三天至上市日之间即可信。
    if (
        listing is not None
        and expected_date - timedelta(days=3) <= announced_date <= listing
    ):
        return
    raise ProviderError(
        f"辉立暗盘PDF日期不匹配：预期{expected_date}，实际{announced_date}"
    )


def parse_phillip_pdf_text(
    text: str,
    code: str,
    grey_date: str,
    source_url: str = "",
    listing_date: str | None = None,
    issue_price: float | None = None,
) -> dict[str, Any]:
    code = _validate_code(code)
    expected_date = date.fromisoformat(_required_date(grey_date, "暗盘"))
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("辉立暗盘PDF未提取到文本")
    normalized = unicodedata.normalize("NFKC", text)

    plain_code = re.escape(str(int(code)))
    # 新版模板代码独占一行；旧版模板写作「股份編號 : 01878」或「(01878)」。
    code_patterns = (
        rf"(?m)^\s*0*{plain_code}\s*$",
        rf"股份編號\s*[:：]?\s*0*{plain_code}(?!\d)",
        rf"\(0*{plain_code}\)",
    )
    if not any(re.search(pattern, normalized) for pattern in code_patterns):
        # 英文版公告模板（Stock code 行 + Hong Kong 日期行）。
        return _parse_phillip_pdf_english(
            normalized, code, expected_date, source_url, listing_date
        )

    # 旧版PDF存在逐字空格（香 港 訊）、年份多一位（20013）或日缺失（10 月日）等笔误。
    announcement = re.search(
        r"香\s*港\s*訊\s*[—–-]\s*(\d{4})\d?\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})?\s*日",
        normalized,
    )
    if not announcement:
        raise ProviderError("辉立暗盘PDF缺少公告日期")
    try:
        year = int(announcement.group(1))
        month = int(announcement.group(2))
        day = announcement.group(3)
        if day is None:
            # 日缺失时仅在年月与文件名日期一致时用文件名日补齐。
            if (year, month) != (expected_date.year, expected_date.month):
                raise ProviderError("辉立暗盘PDF公告日期无效")
            day = expected_date.day
        announced_date = date(year, month, int(day))
    except ValueError as exc:
        raise ProviderError("辉立暗盘PDF公告日期无效") from exc
    if announced_date != expected_date:
        _check_announcement_date(
            announced_date, expected_date, _parse_listing_date(listing_date)
        )

    close: float | None = None
    # 新版官方模板的文本顺序中，三个HKD价格依次为最高价、最低价、最后收市价。
    prices = re.findall(rf"\bHKD\s*({_NUMBER})", normalized)
    if len(prices) >= 3:
        close = _optional_price(prices[-1], "辉立暗盘最后收市")
    else:
        # 旧版模板以「最後收市價 HK$ 122.50」标注；2015年前后的模板把每个词
        # 重复排版四次（「最後最後最後最後 收市價收市價收市價收市價」），用
        # 重复 tolerant 的标签匹配取其后首个 HK$ 价格。
        label_re = re.compile(r"(?:最後)+\s*(?:收市價)+")
        for match in label_re.finditer(normalized):
            window = normalized[match.end() : match.end() + 80]
            price_match = re.search(rf"(?:\bHKD|HK\s*\$)\s*({_NUMBER})", window)
            if price_match:
                close = _optional_price(price_match.group(1), "辉立暗盘最后收市")
                break
    if close is None:
        # 列表区缺失时回退到叙事段「上市前收市價為 HK$X」，并用
        # 「較定價 HK$Y 高/低 Z%」交叉核验（容忍「較定\n價」断行）。
        narrative = re.search(rf"上市前收市價\s*為?\s*HK\s*\$\s*({_NUMBER})", normalized)
        compare = re.search(
            rf"較定\s*價\s*HK\s*\$\s*({_NUMBER})\s*([高低])\s*({_NUMBER})\s*%",
            normalized,
        )
        bases: list[float] = []
        percent = sign = None
        if compare:
            bases.append(float(compare.group(1).replace(",", "")))
            percent = float(compare.group(3).replace(",", ""))
            sign = 1 if compare.group(2) == "高" else -1
        else:
            # 无基数变体「較定價 低 9.18%」：基数取文本招股价或传入的发行价。
            bare = re.search(rf"較定\s*價\s*([高低])\s*({_NUMBER})\s*%", normalized)
            if bare:
                sign = 1 if bare.group(1) == "高" else -1
                percent = float(bare.group(2).replace(",", ""))
                offer = re.search(
                    rf"招股價定為每股\s*HK\s*\$\s*({_NUMBER})", normalized
                )
                if offer:
                    bases.append(float(offer.group(1).replace(",", "")))
                if issue_price:
                    bases.append(float(issue_price))
        if narrative and percent is not None and bases:
            candidate = _optional_price(narrative.group(1), "辉立暗盘最后收市")
            if any(
                base > 0
                and abs((candidate / base - 1) * 100 - sign * percent) < 0.15
                for base in bases
            ):
                close = candidate
    if close is None:
        raise ProviderError("辉立暗盘PDF缺少可核验的最后收市价")
    return {
        "grey_close": close,
        "grey_platform": "辉立",
        "grey_source_url": source_url or None,
    }


_EN_MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}


def _phillip_detected_codes(text: str) -> list[str]:
    """提取辉立PDF实际披露的股票代码，用于校验失败时定位错装文件。"""
    found: list[str] = []
    patterns = (
        r"股份[編编]號\s*[:：]\s*\(?\s*(\d{4,5})\s*\)?(?!\d)",
        r"Stock\s*code\s*[:：]\s*\(?\s*(\d{4,5})\s*\)?(?!\d)",
        r"\((\d{4,5})\)",
    )
    for pattern in patterns:
        for raw in re.findall(pattern, text, re.I):
            detected = f"{int(raw):05d}"
            if detected not in found:
                found.append(detected)
    return found


def _phillip_code_mismatch(text: str, code: str) -> ProviderError:
    """代码不匹配错误；若PDF披露了其他代码，附注以便识别源站错装。"""
    detected = _phillip_detected_codes(text)
    hint = (
        f"（文件内容为{'/'.join(detected)}，疑源站文件覆盖）" if detected else ""
    )
    return ProviderError(f"辉立暗盘PDF股票代码与{code}不匹配{hint}")


def _parse_phillip_pdf_english(
    normalized: str,
    code: str,
    expected_date: date,
    source_url: str,
    listing_date: str | None,
) -> dict[str, Any]:
    """英文版公告：Stock code 行 + (Hong Kong - 17 Nov 2014) 日期行，
    收市价与「Y% higher/lower than its offer price of HK$Z」交叉核验。"""
    plain_code = str(int(code))
    if not re.search(
        rf"Stock\s*code\s*[:：]?\s*0*{re.escape(plain_code)}(?!\d)",
        normalized,
        re.I,
    ):
        raise _phillip_code_mismatch(normalized, code)
    date_match = re.search(
        r"Hong\s*Kong\s*[—–\-]\s*(\d{1,2})\s+([A-Za-z]+)\s*,?\s*(\d{4})",
        normalized,
    )
    if not date_match:
        raise ProviderError("辉立暗盘PDF缺少公告日期")
    month = _EN_MONTHS.get(date_match.group(2)[:3].lower())
    if month is None:
        raise ProviderError("辉立暗盘PDF公告日期无效")
    try:
        announced_date = date(
            int(date_match.group(3)), month, int(date_match.group(1))
        )
    except ValueError as exc:
        raise ProviderError("辉立暗盘PDF公告日期无效") from exc
    if announced_date != expected_date:
        _check_announcement_date(
            announced_date, expected_date, _parse_listing_date(listing_date)
        )
    close_match = re.search(
        rf"closed\s+at\s+HK\$\s*({_NUMBER})\s+per\s+share", normalized, re.I
    )
    compare = re.search(
        rf"({_NUMBER})\s*%\s*(higher|lower)\s+than\s+its\s+of\s*fer\s+price"
        rf"\s+of\s+HK\$\s*({_NUMBER})",
        normalized,
        re.I,
    )
    if not close_match or not compare:
        raise ProviderError("辉立暗盘PDF缺少可核验的最后收市价")
    close = _optional_price(close_match.group(1), "辉立暗盘最后收市")
    base = float(compare.group(3).replace(",", ""))
    percent = float(compare.group(1).replace(",", ""))
    sign = 1 if compare.group(2).lower() == "higher" else -1
    if base <= 0 or abs((close / base - 1) * 100 - sign * percent) >= 0.15:
        raise ProviderError("辉立暗盘PDF涨跌幅与最后收市价不匹配")
    return {
        "grey_close": close,
        "grey_platform": "辉立",
        "grey_source_url": source_url or None,
    }


def parse_phillip_pdf_ocr_text(
    text: str,
    code: str,
    grey_date: str,
    source_url: str = "",
    listing_date: str | None = None,
    issue_price: float | None = None,
) -> dict[str, Any]:
    """解析图片型辉立PDF的OCR文本，要求价格字段与涨跌幅互相核验。"""
    code = _validate_code(code)
    expected_date = date.fromisoformat(_required_date(grey_date, "暗盘"))
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("辉立暗盘PDF未提取到文本")
    # OCR常把"價"识别为"值"，其余字段名可据此还原。
    normalized = unicodedata.normalize("NFKC", text).replace("值", "價")

    plain_code = str(int(code))
    if not re.search(rf"(?m)^\s*0*{re.escape(plain_code)}\s*$", normalized):
        raise _phillip_code_mismatch(normalized, code)

    # 交易终端截图型PDF：无日期行，用「現價/前收市價/升跌%」三字段互相核验，
    # 日期直接采信文件名（URL由上市日前一日探测得出）。
    spot = re.search(rf"現價\s*({_NUMBER})", normalized)
    prev = re.search(rf"前收市價\s*({_NUMBER})", normalized)
    change = re.search(rf"升跌\s*%?\s*([-+−])?\s*({_NUMBER})\s*%", normalized)
    if spot and prev and change:
        price = _optional_price(spot.group(1), "辉立暗盘现价")
        previous = float(prev.group(1).replace(",", ""))
        percent = float(change.group(2).replace(",", ""))
        sign = -1 if change.group(1) in ("-", "−") else 1
        if (
            previous > 0
            and abs((price / previous - 1) * 100 - sign * percent) < 0.15
        ):
            return {
                "grey_close": price,
                "grey_platform": "辉立",
                "grey_source_url": source_url or None,
            }

    announcement = re.search(
        r"香\s*港\s*訊\s*[—–\-一]\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日",
        normalized,
    )
    if not announcement:
        raise ProviderError("辉立暗盘PDF缺少公告日期")
    try:
        announced_date = date(*(int(part) for part in announcement.groups()))
    except ValueError as exc:
        raise ProviderError("辉立暗盘PDF公告日期无效") from exc
    if announced_date != expected_date:
        _check_announcement_date(
            announced_date, expected_date, _parse_listing_date(listing_date)
        )

    if "招股價" in normalized and "最後收市價" in normalized:
        start = normalized.find("招股價")
        end = normalized.find("成交", start)
        block = normalized[start : end if end > start else len(normalized)]
        # 官方模板顺序：招股价、开市价、最高价、最低价、最后收市价。
        prices = [
            float(value.replace(",", ""))
            for value in re.findall(rf"HKD\s*({_NUMBER})", block)
        ]
        if len(prices) >= 5:
            issue_price, close = prices[0], prices[4]
            if issue_price <= 0 or close <= 0:
                raise ProviderError("辉立暗盘PDF价格必须为正数")
            # OCR可能误读数字，用官方模板自带的涨跌幅反查收盘价；
            # 跌幅必须带负号，涨幅则不得带负号。
            expected_percent = (close / issue_price - 1) * 100
            if expected_percent < 0:
                sign_re = r"[-−]\s*"
            else:
                sign_re = r"(?<![-−\d.])"
            if not re.search(rf"{sign_re}{abs(expected_percent):.2f}", normalized):
                raise ProviderError(
                    f"辉立暗盘PDF涨跌幅与最后收市价不匹配：{expected_percent:.2f}%"
                )
            return {
                "grey_close": close,
                "grey_platform": "辉立",
                "grey_source_url": source_url or None,
            }

    # 旧版叙事模板兜底：「上市前收市價HK$X」与「較定價HK$Y 高/低Z%」交叉核验，
    # 容忍OCR噪声（「较」简体、百分比前的多余负号如「低-6.02%」）。
    narrative = re.search(rf"上市前收市價\s*為?\s*HK\$\s*({_NUMBER})", normalized)
    compare = re.search(
        rf"[較较]定\s*價\s*HK\$\s*({_NUMBER})\s*([高低])\s*[-−]?\s*({_NUMBER})\s*%",
        normalized,
    )
    if narrative and compare:
        candidate = _optional_price(narrative.group(1), "辉立暗盘最后收市")
        base = float(compare.group(1).replace(",", ""))
        sign = 1 if compare.group(2) == "高" else -1
        percent = float(compare.group(3).replace(",", ""))
        if base > 0 and abs((candidate / base - 1) * 100 - sign * percent) < 0.15:
            return {
                "grey_close": candidate,
                "grey_platform": "辉立",
                "grey_source_url": source_url or None,
            }
    raise ProviderError("辉立暗盘PDF缺少可核验的最后收市价")


def _ocr_pdf_text(reader: PdfReader) -> str:
    """图片型PDF没有文本层时，逐页做本地中文OCR。"""
    try:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise ProviderError(
            "辉立暗盘PDF为图片型且未安装OCR依赖 rapidocr_onnxruntime"
        ) from exc

    engine = RapidOCR()
    lines: list[str] = []
    for page in reader.pages:
        for image in page.images:
            frame = np.asarray(image.image.convert("RGB"))
            result, _ = engine(frame)
            if result:
                lines.extend(item[1] for item in result)
    return "\n".join(lines)


def _ocr_rendered_pdf_text(content: bytes) -> str | None:
    """CID乱码PDF的文本层不可读，用pymupdf整页渲染后再OCR。未安装时返回None。"""
    try:
        import fitz
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    engine = RapidOCR()
    lines: list[str] = []
    with fitz.open(stream=content, filetype="pdf") as doc:
        for page in doc:
            pixmap = page.get_pixmap(dpi=200)
            frame = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, pixmap.n
            )
            if pixmap.n == 1:
                frame = np.repeat(frame, 3, axis=2)
            elif pixmap.n == 4:
                frame = frame[:, :, :3]
            result, _ = engine(np.ascontiguousarray(frame[:, :, :3]))
            if result:
                lines.extend(item[1] for item in result)
    return "\n".join(lines) or None


def parse_phillip_pdf(
    content: bytes,
    code: str,
    grey_date: str,
    source_url: str = "",
    listing_date: str | None = None,
    issue_price: float | None = None,
) -> dict[str, Any]:
    if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
        raise ProviderError("辉立暗盘文件不是有效PDF")
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise ProviderError(f"辉立暗盘PDF解析失败：{exc}") from exc
    if not text.strip():
        text = _ocr_pdf_text(reader)
        return parse_phillip_pdf_ocr_text(
            text, code, grey_date, source_url, listing_date, issue_price
        )
    try:
        return parse_phillip_pdf_text(
            text, code, grey_date, source_url, listing_date, issue_price
        )
    except ProviderError as exc:
        # CID乱码等文本层不可读的PDF，整页渲染OCR后重试；仍失败则保留原错误。
        rendered = _ocr_rendered_pdf_text(content)
        if rendered:
            try:
                return parse_phillip_pdf_ocr_text(
                    rendered, code, grey_date, source_url, listing_date, issue_price
                )
            except ProviderError:
                pass
        raise exc


class GelonghuiProvider(_AsyncProvider):
    SEARCH_URL = "https://www.gelonghui.com/api/live/search/v2"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(client, "格隆汇快讯", "https://www.gelonghui.com/")
        self._headers.update(
            {"platform": "web", "shortVersion": "10.7.0", "Client-Lang": "zh-cn"}
        )

    @staticmethod
    def _empty(
        source_url: str | None = None, error: str | None = None
    ) -> dict[str, Any]:
        return {
            "grey_close": None,
            "grey_platform": None,
            "grey_source_url": source_url,
            "grey_status": "not_found",
            "grey_error": error,
        }

    async def fetch_grey_market(
        self, code: str, name: str, listing_date: str, issue_price: float | None = None
    ) -> dict:
        try:
            code = _validate_code(code)
            listing = date.fromisoformat(_required_date(listing_date, "上市"))
            name = name.strip() if isinstance(name, str) else ""
            if not name:
                raise ProviderError("格隆汇暗盘检索缺少股票名称")

            start = int(
                datetime.combine(
                    listing - timedelta(days=10), datetime.min.time(), tzinfo=_HK_TZ
                ).timestamp()
            )
            end = int(
                datetime.combine(
                    listing + timedelta(days=1), datetime.min.time(), tzinfo=_HK_TZ
                ).timestamp()
            )
            parsed_results: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            last_url = None
            for term in _grey_search_terms(name):
                payload = await self._get_json(
                    self.SEARCH_URL,
                    {
                        "keyword": term,
                        "startTimestamp": start,
                        "endTimestamp": end,
                        "page": 1,
                        "count": 100,
                    },
                )
                if payload.get("statusCode") != 200:
                    raise ProviderError(
                        f"格隆汇快讯返回失败：statusCode={payload.get('statusCode')!r}"
                    )
                items = payload.get("result") or []
                if not isinstance(items, list):
                    raise ProviderError("格隆汇快讯缺少result数组")

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    title = item.get("title") or ""
                    content = item.get("content") or ""
                    if not isinstance(title, str) or not isinstance(content, str):
                        continue
                    if not _DARK_RE.search(f"{title} {content}"):
                        continue
                    stocks = item.get("stocks") or []
                    if not isinstance(stocks, list):
                        continue
                    matching_stocks = [
                        stock
                        for stock in stocks
                        if isinstance(stock, dict)
                        and stock.get("market") == "HK"
                        and stock.get("code") == code
                    ]
                    if not matching_stocks:
                        continue
                    live_id = item.get("id")
                    if isinstance(live_id, bool) or not (
                        isinstance(live_id, int)
                        or (isinstance(live_id, str) and live_id.isdigit())
                    ):
                        continue
                    live_id = str(live_id)
                    if live_id in seen_ids:
                        continue
                    seen_ids.add(live_id)
                    last_url = (
                        "https://m.gelonghui.com/live?"
                        f"liveId={live_id}&type=HKStock"
                    )

                    hk_codes = {
                        stock.get("code")
                        for stock in stocks
                        if isinstance(stock, dict) and stock.get("market") == "HK"
                    }
                    if len(hk_codes) > 1:
                        overall_platforms = {
                            "富途" if value == "富途" else "辉立"
                            for value in _PLATFORM_RE.findall(content)
                        }
                        names = {name}
                        for stock in matching_stocks:
                            stock_name = stock.get("name")
                            if isinstance(stock_name, str):
                                names.add(stock_name)
                                names.update(_grey_search_terms(stock_name))
                        aliases = {
                            normalized
                            for value in names
                            if (normalized := _normalized_match_text(value))
                        }

                        def belongs_to_stock(clause: str) -> bool:
                            normalized = _normalized_match_text(clause)
                            if any(alias in normalized for alias in aliases):
                                return True
                            plain_code = str(int(code))
                            return bool(
                                re.search(
                                    rf"(?<!\d)(?:{code}|0*{plain_code})(?:\.HK)?(?!\d)",
                                    clause,
                                    re.I,
                                )
                            )

                        clauses = re.split(r"(?<=[，,；;。\n])", content)
                        matched = [
                            clause.strip()
                            for clause in clauses
                            if belongs_to_stock(clause)
                        ]
                        if not matched:
                            continue

                        if len(overall_platforms) == 1:
                            platform = next(iter(overall_platforms))
                            content = (
                                f"{platform}暗盘 "
                                + " ".join(matched)
                            )
                        else:
                            platforms = {
                                "富途" if value == "富途" else "辉立"
                                for value in _PLATFORM_RE.findall(
                                    " ".join(matched)
                                )
                            }
                            if not platforms:
                                platforms = overall_platforms
                            if len(platforms) != 1:
                                continue
                            content = (
                                f"{next(iter(platforms))} "
                                + " ".join(matched)
                            )
                            title = ""

                    document = (
                        "<html><head><title>"
                        + html.escape(title)
                        + "</title></head><body><div id='spanContent'>"
                        + html.escape(content)
                        + "</div></body></html>"
                    )
                    parsed = parse_grey_article(document, last_url)
                    if parsed["grey_close"] is not None:
                        parsed_results.append(parsed)
                    else:
                        unlabeled = parse_unlabeled_grey_close(
                            f"{title} {content}", last_url
                        )
                        if unlabeled is not None:
                            parsed_results.append(unlabeled)

            for platform in ("富途", "辉立", _THIRD_PARTY_PLATFORM):
                for parsed in parsed_results:
                    if parsed["grey_platform"] == platform:
                        return {
                            **parsed,
                            "grey_status": "ok",
                            "grey_error": None,
                        }
            if last_url:
                return self._empty(last_url, "格隆汇快讯未包含可核验的暗盘收盘价")
            return self._empty()
        except ProviderError as exc:
            return self._empty(error=str(exc))


class PhillipGreyProvider(_AsyncProvider):
    BASE_URL = "https://research.cyberquote.com.hk/phillipMart/Files"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(client, "辉立交易场", "https://www.phillip.com.hk/")

    @staticmethod
    def _empty(
        source_url: str | None = None, error: str | None = None
    ) -> dict[str, Any]:
        return {
            "grey_close": None,
            "grey_platform": None,
            "grey_source_url": source_url,
            "grey_status": "not_found",
            "grey_error": error,
        }

    async def fetch_grey_market(
        self, code: str, name: str, listing_date: str, issue_price: float | None = None
    ) -> dict:
        try:
            code = _validate_code(code)
            listing = date.fromisoformat(_required_date(listing_date, "上市"))
        except ProviderError as exc:
            return self._empty(error=str(exc))

        candidate = listing - timedelta(days=1)
        weekday_attempts = 0
        # 覆盖周末及常见港股公众假期，不无限向前探测历史文件。
        while weekday_attempts < 6:
            if candidate.weekday() >= 5:
                candidate -= timedelta(days=1)
                continue
            weekday_attempts += 1
            url = f"{self.BASE_URL}/{candidate:%Y%m%d}-{code}C_Final.pdf"
            try:
                response = await self._get(url)
            except ProviderError as exc:
                if "HTTP 404" in str(exc):
                    candidate -= timedelta(days=1)
                    continue
                return self._empty(error=str(exc))

            try:
                parsed = parse_phillip_pdf(
                    response.content,
                    code,
                    candidate.isoformat(),
                    str(response.url),
                    listing_date=listing.isoformat(),
                    issue_price=issue_price,
                )
            except ProviderError as exc:
                return self._empty(str(response.url), str(exc))
            return {**parsed, "grey_status": "ok", "grey_error": None}
        return self._empty()


class GreyMarketProvider:
    def __init__(self, primary: Any, fallback: Any) -> None:
        self.primary = primary
        self.fallback = fallback

    async def fetch_grey_market(
        self, code: str, name: str, listing_date: str, issue_price: float | None = None
    ) -> dict:
        primary = await self.primary.fetch_grey_market(
            code, name, listing_date, issue_price
        )
        if primary.get("grey_status") == "ok" and primary.get("grey_close") is not None:
            return primary

        fallback = await self.fallback.fetch_grey_market(
            code, name, listing_date, issue_price
        )
        if fallback.get("grey_status") == "ok" and fallback.get("grey_close") is not None:
            return fallback

        errors = list(
            dict.fromkeys(
                error
                for error in (primary.get("grey_error"), fallback.get("grey_error"))
                if error
            )
        )
        return {
            "grey_close": None,
            "grey_platform": None,
            "grey_source_url": fallback.get("grey_source_url")
            or primary.get("grey_source_url"),
            "grey_status": "not_found",
            "grey_error": "；".join(errors) or None,
        }
