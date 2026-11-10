from datetime import datetime, timezone

import httpx
import pytest

from app.providers import (
    AAStocksProvider,
    EastmoneyProvider,
    GelonghuiProvider,
    GreyMarketProvider,
    PhillipGreyProvider,
    ProviderError,
    _grey_search_terms,
    parse_price_history,
    parse_grey_article,
    parse_issue_info,
    parse_phillip_pdf,
    parse_phillip_pdf_ocr_text,
    parse_phillip_pdf_text,
)


def f10_payload(
    price=90, listing_date="2023-12-29 00:00:00", code="09880.HK", issue_way="公开发售,配售"
):
    return {
        "success": True,
        "code": 0,
        "message": "ok",
        "result": {
            "pages": 1,
            "data": [
                {
                    "SECUCODE": code,
                    "LISTING_DATE": listing_date,
                    "ISSUE_WAY": issue_way,
                    "ISSUE_PRICE": price,
                }
            ],
        },
    }


def kline_payload(close="90.850", day="2023-12-29", high="93.450", low="88.250"):
    return {
        "rc": 0,
        "data": {
            "code": "09880",
            "klines": [
                f"{day},89.900,{close},{high},{low},1657500,149800267.000,5.78,0.94,0.850,1.41"
            ],
        },
    }


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_parse_issue_info_and_nullable_fields():
    assert parse_issue_info(f10_payload()) == {
        "issue_price": 90.0,
        "listing_date": "2023-12-29",
        "issue_way": "公开发售,配售",
    }
    assert parse_issue_info(f10_payload(price="-", listing_date="-")) == {
        "issue_price": None,
        "listing_date": None,
        "issue_way": "公开发售,配售",
    }
    assert parse_issue_info(f10_payload(price="-", listing_date="-", issue_way=" - ")) == {
        "issue_price": None,
        "listing_date": None,
        "issue_way": None,
    }


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"success": False, "code": 1, "message": "bad"},
        {"success": True, "code": 0, "result": {"data": []}},
        f10_payload(price="not-a-number"),
        f10_payload(price=-1),
        f10_payload(listing_date="2023-02-30 00:00:00"),
        f10_payload(listing_date="29/12/2023"),
    ],
)
def test_parse_issue_info_rejects_bad_payloads(payload):
    with pytest.raises(ProviderError):
        parse_issue_info(payload)


def test_parse_price_history():
    assert parse_price_history(kline_payload()) == {
        "first_close": 90.85,
        "first_trade_date": "2023-12-29",
        "historical_low": 88.25,
        "historical_high": 93.45,
    }


def test_parse_price_history_uses_all_daily_extremes():
    payload = kline_payload()
    payload["data"]["klines"].extend(
        [
            "2024-01-02,91.000,95.000,120.500,89.000,1,1,0,0,0,0",
            "2024-01-03,95.000,96.000,100.000,80.250,1,1,0,0,0,0",
        ]
    )
    result = parse_price_history(payload)
    assert result["historical_low"] == 80.25
    assert result["historical_high"] == 120.5


@pytest.mark.parametrize(
    "payload",
    [
        {"rc": 1, "data": None},
        {"rc": 0, "data": {"klines": []}},
        {"rc": 0, "data": {"klines": ["2023-12-29,89.9"]}},
        kline_payload(close="-"),
        kline_payload(close="nan"),
        kline_payload(high="-"),
        kline_payload(low="nan"),
        kline_payload(high="80", low="90"),
        kline_payload(day="2023-02-30"),
    ],
)
def test_parse_price_history_rejects_bad_payloads(payload):
    with pytest.raises(ProviderError):
        parse_price_history(payload)


@pytest.mark.asyncio
async def test_fetch_issue_info_checks_code_and_query():
    def handler(request):
        assert request.url.params["filter"] == '(SECUCODE="09880.HK")'
        assert request.url.params["reportName"] == "RPT_HSPE_ISSUEINFO"
        return httpx.Response(200, json=f10_payload())

    async with mock_client(handler) as client:
        result = await EastmoneyProvider(client).fetch_issue_info("09880")
    assert result["issue_price"] == 90.0
    assert result["listing_date"] == "2023-12-29"
    assert result["issue_way"] == "公开发售,配售"


@pytest.mark.asyncio
async def test_fetch_issue_info_rejects_mismatched_code():
    async with mock_client(lambda request: httpx.Response(200, json=f10_payload(code="00700.HK"))) as client:
        with pytest.raises(ProviderError, match="代码不匹配"):
            await EastmoneyProvider(client).fetch_issue_info("09880")


@pytest.mark.asyncio
async def test_fetch_price_history_uses_unadjusted_history_since_listing():
    def handler(request):
        assert request.url.params["secid"] == "116.09880"
        assert request.url.params["fqt"] == "0"
        assert request.url.params["beg"] == "20231229"
        assert request.url.params["end"] == "20500101"
        return httpx.Response(200, json=kline_payload())

    async with mock_client(handler) as client:
        result = await EastmoneyProvider(client).fetch_price_history("09880", "2023-12-29")
    assert result == {
        "first_close": 90.85,
        "first_trade_date": "2023-12-29",
        "historical_low": 88.25,
        "historical_high": 93.45,
    }


@pytest.mark.asyncio
async def test_fetch_price_history_falls_back_to_tencent():
    def handler(request):
        if request.url.host == "push2his.eastmoney.com":
            return httpx.Response(403, text="forbidden")
        assert request.url.host == "web.ifzq.gtimg.cn"
        assert request.url.params["param"] == (
            "hk09880,day,2023-12-29,2050-01-01,2000"
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "hk09880": {
                        "day": [
                            ["2023-12-29", "89.900", "90.850", "93.450", "88.250", "1"],
                            ["2024-01-02", "90.700", "89.100", "120.500", "80.250", "1"],
                        ]
                    }
                },
            },
        )

    async with mock_client(handler) as client:
        result = await EastmoneyProvider(client).fetch_price_history(
            "09880", "2023-12-29"
        )
    assert result == {
        "first_close": 90.85,
        "first_trade_date": "2023-12-29",
        "historical_low": 80.25,
        "historical_high": 120.5,
    }


@pytest.mark.asyncio
async def test_fetch_price_history_rejects_wrong_trade_date():
    async with mock_client(
        lambda request: httpx.Response(200, json=kline_payload(day="2023-12-28"))
    ) as client:
        with pytest.raises(ProviderError, match="上市日不匹配"):
            await EastmoneyProvider(client).fetch_price_history("09880", "2023-12-29")


@pytest.mark.asyncio
async def test_fetch_price_history_accepts_delayed_first_trading_day():
    async with mock_client(
        lambda request: httpx.Response(200, json=kline_payload(day="2024-01-02"))
    ) as client:
        result = await EastmoneyProvider(client).fetch_price_history(
            "09880", "2023-12-29"
        )
    assert result["first_trade_date"] == "2024-01-02"


def ipo_html(rows, pagination=""):
    body = "".join(
        f"<tr><td>{index}</td><td><a>{code}</a></td><td><a>{name}</a></td>"
        f"<td>1</td><td>1</td><td>1</td><td>-</td><td>{listing_date}</td></tr>"
        for index, (code, name, listing_date) in enumerate(rows, start=1)
    )
    return f"""
    <html><body><table class="table table_striped">
      <thead><tr><td>序号</td><td>股票代码</td><td>股票名称</td><td>招股价</td>
      <td>招股数(股)</td><td>募集资金(港元)</td><td>招股日期</td><td>上市日期</td></tr></thead>
      <tbody>{body}</tbody>
    </table><nav>{pagination}</nav></body></html>
    """


@pytest.mark.asyncio
async def test_fetch_ipo_list_follows_real_html_pagination():
    first = ipo_html(
        [("09880", "优必选", "2023-12-29"), ("02384", "待上市公司", "-")],
        '<a href="ipolist_2.html?code=&amp;startdate=&amp;enddate=">2</a>',
    )
    second = ipo_html([("00700", "腾讯控股", "1998-06-16")])
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, text=second if request.url.path.endswith("_2.html") else first)

    async with mock_client(handler) as client:
        records = await EastmoneyProvider(client).fetch_ipo_list()
    assert paths == ["/ipolist.html", "/ipolist_2.html"]
    assert records == [
        {"code": "09880", "name": "优必选", "listing_date": "2023-12-29"},
        {"code": "02384", "name": "待上市公司", "listing_date": None},
        {"code": "00700", "name": "腾讯控股", "listing_date": "1998-06-16"},
    ]


@pytest.mark.asyncio
async def test_fetch_ipo_list_rejects_changed_html():
    async with mock_client(lambda request: httpx.Response(200, text="<html>暂无数据</html>")) as client:
        with pytest.raises(ProviderError, match="目标表格"):
            await EastmoneyProvider(client).fetch_ipo_list()


@pytest.mark.asyncio
async def test_fetch_latest_quotes_uses_ulist_f124_and_nulls():
    epoch = int(datetime(2024, 1, 2, 8, 0, tzinfo=timezone.utc).timestamp())

    def handler(request):
        assert request.url.path == "/api/qt/ulist.np/get"
        assert request.url.params["secids"] == "116.09880,116.00700,116.09999"
        assert "f124" in request.url.params["fields"]
        return httpx.Response(
            200,
            json={
                "rc": 0,
                "data": {
                    "diff": [
                        {"f2": 63.65, "f12": "09880", "f124": epoch},
                        {"f2": "-", "f12": "00700", "f124": "-"},
                    ]
                },
            },
        )

    async with mock_client(handler) as client:
        quotes = await EastmoneyProvider(client).fetch_latest_quotes(
            ["09880", "00700", "09999"]
        )
    assert quotes["09880"] == {
        "latest_price": 63.65,
        "quote_date": "2024-01-02",
        "quote_time": "16:00:00",
    }
    assert quotes["00700"] == {
        "latest_price": None,
        "quote_date": None,
        "quote_time": None,
    }
    assert quotes["09999"] == {
        "latest_price": None,
        "quote_date": None,
        "quote_time": None,
    }


@pytest.mark.asyncio
async def test_fetch_latest_quotes_falls_back_to_tencent():
    fields = ["100", "UBTECH", "09880", "63.650"] + ["0"] * 26
    fields.append("2026/11/05 16:00:00")

    def handler(request):
        if request.url.host == "push2.eastmoney.com":
            return httpx.Response(403, text="forbidden")
        assert request.url.path == "/q=hk09880"
        return httpx.Response(
            200,
            content=f'v_hk09880="{"~".join(fields)}";'.encode("gbk"),
        )

    async with mock_client(handler) as client:
        quotes = await EastmoneyProvider(client).fetch_latest_quotes(["09880"])
    assert quotes["09880"] == {
        "latest_price": 63.65,
        "quote_date": "2026-11-05",
        "quote_time": "16:00:00",
    }


@pytest.mark.asyncio
async def test_fetch_latest_quotes_empty_and_invalid_codes():
    async with mock_client(lambda request: pytest.fail("空代码列表不应请求网络")) as client:
        provider = EastmoneyProvider(client)
        assert await provider.fetch_latest_quotes([]) == {}
        with pytest.raises(ProviderError, match="5位数字"):
            await provider.fetch_latest_quotes(["9880"])


@pytest.mark.asyncio
async def test_request_retries_transient_failure(monkeypatch):
    attempts = 0

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("app.providers.asyncio.sleep", no_sleep)

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json=f10_payload())

    async with mock_client(handler) as client:
        result = await EastmoneyProvider(client).fetch_issue_info("09880")
    assert result["issue_price"] == 90.0
    assert attempts == 3


@pytest.mark.asyncio
async def test_request_does_not_bypass_403():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="forbidden")

    async with mock_client(handler) as client:
        with pytest.raises(ProviderError, match="HTTP 403"):
            await EastmoneyProvider(client).fetch_issue_info("09880")
    assert calls == 1


def test_parse_grey_article_futu_has_priority_over_phillip():
    html = """
    <div id="spanContent"><p>
      根据富途交易平台显示，东山精密(02384.HK)暗盘低开9.1%报78.9元，
      最高/低分别见82/70.5元，收报80.35元，较上市价低7.4%。
      <br>根据辉立交易平台显示，东山精密(02384.HK)暗盘开报80元，收市报82元。
    </p></div>
    """
    assert parse_grey_article(html, "https://www.aastocks.com/article") == {
        "grey_close": 80.35,
        "grey_platform": "富途",
        "grey_source_url": "https://www.aastocks.com/article",
    }


def test_parse_grey_article_phillip_fallback_and_traditional_wording():
    html = """
    <div id="spanContent">
      <p>根据富途交易平台显示，某股(00001.HK)暗盘开报10元，最高11元。</p>
      <p>根据輝立交易場資料顯示，某股(00001.HK)暗盤收市報9.95港元。</p>
    </div>
    """
    result = parse_grey_article(html)
    assert result["grey_close"] == 9.95
    assert result["grey_platform"] == "辉立"
    assert result["grey_source_url"] is None


@pytest.mark.parametrize(
    "text",
    [
        "根据辉立交易平台显示，暗盘平开报90元，高低波幅81.05至92元。",
        "根据富途交易平台显示，暗盘收报N/A。",
        "根据辉立交易平台显示，暗盘收报-1元。",
        "根据富途交易平台显示，上市首日收报90元。",
    ],
)
def test_parse_grey_article_does_not_invent_close(text):
    result = parse_grey_article(f'<div id="spanContent">{text}</div>')
    assert result["grey_close"] is None
    assert result["grey_platform"] is None


def test_parse_grey_article_accepts_close_after_change_percentage():
    article = (
        '<div id="spanContent">富途行情显示，该股暗盘段收涨53.77%，报147港元。</div>'
    )
    result = parse_grey_article(article, "https://example.com/gelonghui")
    assert result == {
        "grey_close": 147.0,
        "grey_platform": "富途",
        "grey_source_url": "https://example.com/gelonghui",
    }


@pytest.mark.parametrize(
    "text",
    [
        "富途行情显示，该股暗盘一度收涨53.77%，报147港元。",
        "富途行情显示，该股暗盘盘中收涨53.77%，报147港元。",
        "富途行情显示，该股暗盘段收涨53.77%，现报147港元。",
        "富途行情显示，该股暗盘段收涨53.77%，暂报147港元。",
        "富途行情显示，该股暗盘段收涨53.77%，最新报147港元。",
    ],
)
def test_parse_grey_article_rejects_intraday_change_wording(text):
    result = parse_grey_article(f'<div id="spanContent">{text}</div>')
    assert result["grey_close"] is None
    assert result["grey_platform"] is None


@pytest.mark.asyncio
async def test_aastocks_name_search_and_real_article_url():
    search = """
    <a id="cp_ucAAFNSearch_repNews_lnkNews_0"
       href="/sc/stocks/news/aafn-con/NOW.1316180/ipo-news/AAFN">
       《新股表现》优必选(09880.HK)暗盘收89.95元</a>
    """
    article = """
    <div id="spanContent">根据辉立交易场资料显示，优必选(09880.HK)
    暗盘平开报90元，收报89.95元。</div>
    """

    def handler(request):
        if request.url.path.endswith("/ipo-news/0"):
            assert request.url.params["key"] == "优必选"
            return httpx.Response(200, text=search)
        return httpx.Response(200, text=article)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "09880", "优必选", "2023-12-29"
        )
    assert result == {
        "grey_close": 89.95,
        "grey_platform": "辉立",
        "grey_source_url": (
            "https://www.aastocks.com/sc/stocks/news/aafn-con/NOW.1316180/ipo-news/AAFN"
        ),
        "grey_status": "ok",
        "grey_error": None,
    }


@pytest.mark.asyncio
async def test_aastocks_not_found_and_request_error_are_honest():
    async with mock_client(lambda request: httpx.Response(200, text="<html>无结果</html>")) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "09880", "优必选", "2023-12-29"
        )
    assert result["grey_status"] == "not_found"
    assert result["grey_close"] is None
    assert result["grey_error"] is None

    async with mock_client(lambda request: httpx.Response(429, text="limited")) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "09880", "优必选", "2023-12-29"
        )
    assert result["grey_status"] == "not_found"
    assert "HTTP 429" in result["grey_error"]


def test_parse_grey_article_recovers_headline_close_for_first_platform():
    html = """
    <title>《新股》爱芯元智(00600.HK)暗盘收报27元 低上市价4.3%</title>
    <div id="spanContent">
      <p>根据辉立交易平台显示，爱芯元智(00600.HK)暗盘平开报28.2元，
      每手100股，账面蚀120元。</p>
      <p>根据富途交易平台显示，爱芯元智(00600.HK)暗盘平开报28.2元，
      每手100股，账面蚀118元。</p>
    </div>
    """
    result = parse_grey_article(html, "https://example.com/headline")
    assert result["grey_close"] == 27.0
    assert result["grey_platform"] == "辉立"


def test_parse_grey_article_headline_futu_has_priority_over_phillip_body():
    html = """
    <title>《新股》南山铝业国际(02610.HK)暗盘收报26.4元</title>
    <div id="spanContent">
      <p>根据富途交易平台显示，南山铝业国际(02610.HK)暗盘平开报26.6元。</p>
      <p>根据辉立交易平台显示，南山铝业国际(02610.HK)暗盘收报26.65元。</p>
    </div>
    """
    result = parse_grey_article(html)
    assert result["grey_close"] == 26.4
    assert result["grey_platform"] == "富途"


def test_parse_grey_article_headline_requires_verifiable_platform():
    html = """
    <title>某股(00001.HK)暗盘收报10元</title>
    <div id="spanContent">某股暗盘平开报10元，没有平台收盘信息。</div>
    """
    assert parse_grey_article(html)["grey_close"] is None


@pytest.mark.asyncio
async def test_aastocks_falls_back_to_short_chinese_name():
    search = """
    <a href="/sc/stocks/news/aafn-con/NOW.1546769/ipo-news/AAFN">
      本末动力（北京）科技(06731.HK)暗盘收报40元</a>
    """
    article = """
    <div id="spanContent">根据富途交易平台显示，
    本末动力（北京）科技(06731.HK)暗盘收报40元。</div>
    """
    queries = []

    def handler(request):
        if request.url.path.endswith("/ipo-news/0"):
            term = request.url.params["key"]
            queries.append(term)
            return httpx.Response(200, text=search if term == "本末" else "<html>无结果</html>")
        return httpx.Response(200, text=article)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "06731", "本末科技", "2026-09-29"
        )
    assert queries == ["本末科技", "本末"]
    assert result["grey_status"] == "ok"
    assert result["grey_close"] == 40.0


@pytest.mark.asyncio
async def test_aastocks_title_matching_is_case_insensitive():
    search = """
    <a href="/sc/stocks/news/aafn-con/NOW.1/ipo-news/AAFN">
      Senasic暗盘收报20元</a>
    """
    article = """
    <div id="spanContent">根据富途交易平台显示，
    SENASIC(06675.HK)暗盘收报20元。</div>
    """

    def handler(request):
        text = search if request.url.path.endswith("/ipo-news/0") else article
        return httpx.Response(200, text=text)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "06675", "SENASIC", "2026-06-17"
        )
    assert result["grey_status"] == "ok"
    assert result["grey_close"] == 20.0


@pytest.mark.asyncio
async def test_aastocks_dated_non_close_title_can_use_body_close():
    search = """
    <div><div><div>
      <a href="/sc/stocks/news/aafn-con/NOW.1518339/ipo-news/AAFN">
        群核科技(00068.HK)暗盘表现</a>
      <script>ConvertToLocalTime({dt:'2026/04/16 18:35'});</script>
    </div></div></div>
    """
    article = """
    <div id="spanContent">根据富途交易平台显示，
    群核科技(00068.HK)在富途暗盘收报35元。</div>
    """

    def handler(request):
        text = search if request.url.path.endswith("/ipo-news/0") else article
        return httpx.Response(200, text=text)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "00068", "群核科技", "2026-04-17"
        )
    assert result["grey_status"] == "ok"
    assert result["grey_platform"] == "富途"
    assert result["grey_close"] == 35.0


@pytest.mark.asyncio
async def test_aastocks_short_name_never_accepts_mismatched_code():
    search = """
    <a href="/sc/stocks/news/aafn-con/NOW.1/ipo-news/AAFN">
      本末动力暗盘收报40元</a>
    """
    article = """
    <div id="spanContent">根据富途交易平台显示，
    本末动力(09999.HK)暗盘收报40元。</div>
    """

    def handler(request):
        if request.url.path.endswith("/ipo-news/0"):
            text = search if request.url.params["key"] == "本末" else "<html>无结果</html>"
        else:
            text = article
        return httpx.Response(200, text=text)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "06731", "本末科技", "2026-09-29"
        )
    assert result["grey_status"] == "not_found"
    assert result["grey_close"] is None


@pytest.mark.asyncio
async def test_aastocks_accepts_mobile_newscontent_article_url():
    search = """
    <a href="/tc/mobile/newscontent/HK/NOW.1491925">
      《新股》翰思艾泰-B(03378.HK)暗盘收报32.66元</a>
    """
    article = """
    <div id="spanContent">根據富途交易平台顯示，
    翰思艾泰-B(03378.HK)暗盤收報32.66元。</div>
    """

    def handler(request):
        if request.url.path.endswith("/ipo-news/0"):
            return httpx.Response(200, text=search)
        assert request.url.path == "/tc/mobile/newscontent/HK/NOW.1491925"
        return httpx.Response(200, text=article)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "03378", "翰思艾泰-B", "2025-12-23"
        )
    assert result["grey_status"] == "ok"
    assert result["grey_platform"] == "富途"
    assert result["grey_close"] == 32.66
    assert result["grey_source_url"] == (
        "https://www.aastocks.com/tc/mobile/newscontent/HK/NOW.1491925"
    )


@pytest.mark.asyncio
async def test_aastocks_accepts_mobile_newsid_article_url():
    search = """
    <a href="/sc/mobile/news.aspx?newsid=NOW.1491923&amp;newssource=HK6">
      《新股》诺比侃(02635.HK)暗盘收报300.4元</a>
    """
    article = """
    <div id="spanContent">根据富途交易平台显示，
    诺比侃(02635.HK)暗盘收报300.4元。</div>
    """

    def handler(request):
        if request.url.path.endswith("/ipo-news/0"):
            return httpx.Response(200, text=search)
        assert request.url.params["newsid"] == "NOW.1491923"
        return httpx.Response(200, text=article)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "02635", "诺比侃", "2025-12-23"
        )
    assert result["grey_status"] == "ok"
    assert result["grey_platform"] == "富途"
    assert result["grey_close"] == 300.4


@pytest.mark.asyncio
async def test_aastocks_accepts_stock_aafn_con_analysis_url():
    search = """
    <a href="/sc/stocks/analysis/stock-aafn-con/02656/aafn/now.1470691/hk-stock-news">
      《新股》健康160(02656.HK)暗盘收报29元 高上市价143.9%</a>
    """
    article = """
    <div id="spanContent">根据富途交易平台显示，
    健康160(02656.HK)暗盘收报29元。</div>
    """

    def handler(request):
        if request.url.path.endswith("/ipo-news/0"):
            return httpx.Response(200, text=search)
        return httpx.Response(200, text=article)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "02656", "健康160", "2025-09-17"
        )
    assert result["grey_status"] == "ok"
    assert result["grey_platform"] == "富途"
    assert result["grey_close"] == 29.0


def test_aastocks_search_timeout_tolerates_slow_page():
    assert AAStocksProvider.SEARCH_TIMEOUT.read >= 30.0


def test_grey_search_terms_add_fullwidth_digit_variants():
    assert _grey_search_terms("健康160") == ["健康160", "健康１６０"]
    assert _grey_search_terms("翰思艾泰-B") == ["翰思艾泰-B", "翰思艾泰"]


@pytest.mark.asyncio
async def test_aastocks_fullwidth_digit_variant_recovers_grey_article():
    empty_search = "<html><body>没有符合的结果</body></html>"
    variant_search = """
    <div><div><div>
      <a href="/sc/stocks/news/aafn-con/NOW.1470691/ipo-news/AAFN">
        《新股》健康１６０(02656.HK)暗盘收报29元 高上市价143.9%</a>
      <script>ConvertToLocalTime({dt:'2025/09/16 20:45'});</script>
    </div></div></div>
    """
    article = """
    <div id="spanContent">
      <p>富途交易平台显示，明天挂牌的健康１６０(02656.HK)暗盘高开0.9%报12元，
      最高/低分别见35.48/11.89元，收报28.9元，较上市价高143.1%。</p>
      <p>根据辉立交易平台显示，健康１６０(02656.HK)暗盘收报29元。</p>
    </div>
    """
    queries = []

    def handler(request):
        if request.url.path.endswith("/ipo-news/0"):
            term = request.url.params["key"]
            queries.append(term)
            text = variant_search if term == "健康１６０" else empty_search
            return httpx.Response(200, text=text)
        return httpx.Response(200, text=article)

    async with mock_client(handler) as client:
        result = await AAStocksProvider(client).fetch_grey_market(
            "02656", "健康160", "2025-09-17"
        )
    assert queries == ["健康160", "健康１６０"]
    assert result["grey_status"] == "ok"
    assert result["grey_platform"] == "富途"
    assert result["grey_close"] == 28.9


@pytest.mark.asyncio
async def test_gelonghui_extracts_futu_close_and_verifies_stock():
    payload = {
        "statusCode": 200,
        "totalCount": 2,
        "result": [
            {
                "id": 2119923,
                "title": "云迹暗盘",
                "content": "辉立行情显示，云迹暗盘收报146港元。",
                "stocks": [{"market": "HK", "code": "02670"}],
            },
            {
                "id": 2119924,
                "title": "云迹暗盘涨53.77%",
                "content": "富途行情显示，该股暗盘段收涨53.77%，报147港元。",
                "stocks": [{"market": "HK", "code": "02670"}],
            },
        ],
    }

    def handler(request):
        assert request.url.path == "/api/live/search/v2"
        assert request.url.params["keyword"] == "云迹"
        assert request.headers["platform"] == "web"
        return httpx.Response(200, json=payload)

    async with mock_client(handler) as client:
        result = await GelonghuiProvider(client).fetch_grey_market(
            "02670", "云迹", "2025-10-16"
        )
    assert result == {
        "grey_close": 147.0,
        "grey_platform": "富途",
        "grey_source_url": "https://m.gelonghui.com/live?liveId=2119924&type=HKStock",
        "grey_status": "ok",
        "grey_error": None,
    }

    payload["result"] = [
        {
            "id": 2119924,
            "title": "其他股票暗盘",
            "content": "富途行情显示，该股暗盘段收涨53.77%，报147港元。",
            "stocks": [{"market": "HK", "code": "09999"}],
        }
    ]
    async with mock_client(handler) as client:
        result = await GelonghuiProvider(client).fetch_grey_market(
            "02670", "云迹", "2025-10-16"
        )
    assert result["grey_status"] == "not_found"
    assert result["grey_close"] is None


@pytest.mark.asyncio
async def test_gelonghui_accepts_unlabeled_close_as_third_party_source():
    payload = {
        "statusCode": 200,
        "result": [
            {
                "id": 612391,
                "title": "【环龙控股(2260.HK)暗盘收涨7.38%】",
                "content": (
                    "格隆汇1月10日丨环龙控股(2260.HK)暗盘收涨7.38%报1.31港元，"
                    "成交405.47万港元，市值6.29亿港元。"
                ),
                "stocks": [{"market": "HK", "code": "02260"}],
            }
        ],
    }

    async with mock_client(lambda request: httpx.Response(200, json=payload)) as client:
        result = await GelonghuiProvider(client).fetch_grey_market(
            "02260", "环龙控股", "2022-01-11"
        )
    assert result["grey_close"] == 1.31
    assert result["grey_platform"] == "格隆汇快讯"
    assert result["grey_status"] == "ok"

    # 带平台字样但只有盘中价时，不得套用第三方口径。
    payload["result"][0] = {
        "id": 627651,
        "title": "【新股暗盘 | 清晰医疗(1406.HK)暗盘段涨逾4%】",
        "content": (
            "格隆汇2月17日丨据富途证券，清晰医疗(1406.HK)暗盘段现报1.67港元，"
            "较发行价1.6港元涨4.38%。"
        ),
        "stocks": [{"market": "HK", "code": "01406"}],
    }
    async with mock_client(lambda request: httpx.Response(200, json=payload)) as client:
        result = await GelonghuiProvider(client).fetch_grey_market(
            "01406", "清晰医疗", "2022-02-18"
        )
    assert result["grey_status"] == "not_found"
    assert result["grey_close"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,name,expected_close",
    [
        ("09696", "天齐锂业", 76.4),
        ("09896", "名创优品", 13.0),
        ("06661", "湖州燃气", 5.74),
        ("06686", "诺亚控股", 278.2),
    ],
)
async def test_gelonghui_multi_stock_flash_assigns_only_own_clause(
    code, name, expected_close
):
    stocks = [
        {"market": "HK", "code": "09696", "name": "天齐锂业"},
        {"market": "HK", "code": "09896", "name": "名创优品"},
        {"market": "HK", "code": "06661", "name": "湖州燃气"},
        {"market": "HK", "code": "06686", "name": "诺亚控股"},
    ]
    payload = {
        "statusCode": 200,
        "result": [
            {
                "id": 739980,
                "title": "【港股明日有四只新股上市 暗盘全部破发】",
                "content": (
                    "格隆汇7月12日丨港股市场明日有四只新股上市，在富途行情上，"
                    "其中天齐锂业暗盘收跌6.83%报76.4港元，"
                    "名创优品暗盘收跌5.8%报13港元，"
                    "湖州燃气暗盘收跌5.59%报5.74港元，"
                    "诺亚控股-S跌4.73%报278.2港元。"
                ),
                "stocks": stocks,
            }
        ],
    }
    async with mock_client(
        lambda request: httpx.Response(200, json=payload)
    ) as client:
        result = await GelonghuiProvider(client).fetch_grey_market(
            code, name, "2022-07-13"
        )
    assert result["grey_close"] == expected_close
    if expected_close is not None:
        assert result["grey_platform"] == "富途"
        assert result["grey_status"] == "ok"


PHILLIP_TEXT = """
新聞稿
2285
HKD
HKD
HKD 51.30
HKD 44.00
HKD 50.25
HKD
「輝立交易場」公佈【香港訊 — 2021年12月29日】
泉峰控股有限公司
招股價
開市價格
最高價格 (與招股價比較)
最低價格 (與招股價比較)
最後收市價 (與招股價比較)
成交股數
(17.66%)
(0.92%)
(15.25%)
43.60
44.00
57,297,560.00
"""


def test_parse_phillip_pdf_extracts_official_close(monkeypatch):
    class Page:
        def extract_text(self):
            return PHILLIP_TEXT

    class Reader:
        pages = [Page()]

    monkeypatch.setattr("app.providers.PdfReader", lambda stream: Reader())
    result = parse_phillip_pdf(
        b"%PDF-fake", "02285", "2021-12-29", "https://example.com/phillip.pdf"
    )
    assert result == {
        "grey_close": 50.25,
        "grey_platform": "辉立",
        "grey_source_url": "https://example.com/phillip.pdf",
    }


@pytest.mark.parametrize(
    "code,grey_date,text",
    [
        ("00020", "2021-12-29", PHILLIP_TEXT),
        ("02285", "2021-12-28", PHILLIP_TEXT),
        ("02285", "2021-12-29", PHILLIP_TEXT.replace("HKD 50.25", "HKD")),
        (
            "02285",
            "2021-12-29",
            PHILLIP_TEXT.replace("HKD 50.25", "HKD").replace("最後收市價", "收市價"),
        ),
    ],
)
def test_parse_phillip_pdf_rejects_unverifiable_fields(code, grey_date, text):
    with pytest.raises(ProviderError):
        parse_phillip_pdf_text(text, code, grey_date)


def test_parse_phillip_pdf_rejects_non_pdf():
    with pytest.raises(ProviderError, match="不是有效PDF"):
        parse_phillip_pdf(b"<html>not found</html>", "02285", "2021-12-29")


PHILLIP_HK_DOLLAR_SPACED_TEXT = """
輝立交易場公布輝立交易場公布輝立交易場公布輝立交易場公布
金嗓子控股集團有限公司金嗓子控股集團有限公司金嗓子控股集團有限公司金嗓子控股集團有限公司(06896) 場外交易價
【 香港訊 -2015 年 7 月 14 日】 輝立証券 (香港 )有限公司 今天公布 金嗓子控股集團有限公司
(「金嗓子」; 股份編號 ： 06896 股份上市前於 輝立交易場 之交易活動詳情 。
經 輝立交易場 買賣之 金嗓子 的最高價為 HK$4.65 及最低價為 HK$4.53 。
上市前收市價為 HK$4.60 跟定價 相同 。
將於 明天 掛牌的 金嗓子 招股價定為每股 HK$4.60 。
9. 最後收市價最後收市價最後收市價最後收市價  HK $4.60
10. 最後收市價與招股價比較最後收市價與招股價比較  相同相同相同相同
"""


def test_parse_phillip_pdf_text_recovers_spaced_hk_dollar_close():
    # 金嗓子(06896)模板「HK $4.60」中HK与$间有空格，且收市价与招股价相同。
    result = parse_phillip_pdf_text(
        PHILLIP_HK_DOLLAR_SPACED_TEXT, "06896", "2015-07-14", issue_price=4.6
    )
    assert result["grey_close"] == 4.6
    assert result["grey_platform"] == "辉立"


PHILLIP_OCR_TEXT = """
辉立证券（香港）有限公司
「辉立交易埸」公怖【香港訊一2022年03月21日】
瑞爾集團有限公司
（股份代号：6639）
招股值
HKD 14.62
開市值格
HKD 13.36
最高值格（與招股值比较）
HKD 14.40
最低值格（與招股值比较）
HKD 13.36
最後收市值（與招股值比较）
HKD 14.36
(-1.78%)
成交股敷
484,500股
6639
瑞爾集团
"""


def test_parse_phillip_pdf_ocr_text_recovers_scanned_close():
    result = parse_phillip_pdf_ocr_text(
        PHILLIP_OCR_TEXT, "06639", "2022-03-21", "https://example.com/06639.pdf"
    )
    assert result == {
        "grey_close": 14.36,
        "grey_platform": "辉立",
        "grey_source_url": "https://example.com/06639.pdf",
    }


@pytest.mark.parametrize(
    "code,grey_date,text",
    [
        ("02656", "2022-03-21", PHILLIP_OCR_TEXT),
        ("06639", "2022-03-22", PHILLIP_OCR_TEXT),
        ("06639", "2022-03-21", PHILLIP_OCR_TEXT.replace("(-1.78%)", "(-1.50%)")),
        ("06639", "2022-03-21", PHILLIP_OCR_TEXT.replace("HKD 14.36", "HKD")),
    ],
)
def test_parse_phillip_pdf_ocr_text_rejects_unverifiable_fields(code, grey_date, text):
    with pytest.raises(ProviderError):
        parse_phillip_pdf_ocr_text(text, code, grey_date)


PHILLIP_EN_TEXT = """
PhillipMart Announces Pre-IPO Share Price of
Wai Chi Holdings Company Limited (Stock code: 01305)
(Hong Kong - 17 Nov 2014) Phillip Securities (HK) Ltd. announced today
the pre-IPO trading price closed at HK$3.34 per share, 9.24% lower than
its of fer price of HK$3.68.
"""


def test_parse_phillip_pdf_text_recovers_english_close():
    result = parse_phillip_pdf_text(
        PHILLIP_EN_TEXT, "01305", "2014-11-17", "https://example.com/01305.pdf"
    )
    assert result == {
        "grey_close": 3.34,
        "grey_platform": "辉立",
        "grey_source_url": "https://example.com/01305.pdf",
    }


@pytest.mark.parametrize(
    "code,grey_date,text",
    [
        ("01273", "2014-11-17", PHILLIP_EN_TEXT),
        ("01305", "2014-11-18", PHILLIP_EN_TEXT),
        (
            "01305",
            "2014-11-17",
            PHILLIP_EN_TEXT.replace("9.24% lower", "8.24% lower"),
        ),
        (
            "01305",
            "2014-11-17",
            PHILLIP_EN_TEXT.replace("HK$3.34 per share", "HK$ per share"),
        ),
    ],
)
def test_parse_phillip_pdf_text_rejects_unverifiable_english(code, grey_date, text):
    with pytest.raises(ProviderError):
        parse_phillip_pdf_text(text, code, grey_date)


def test_parse_phillip_pdf_text_reports_detected_code_on_mismatch():
    """代码不符时附注PDF实际披露的代码，便于识别源站文件覆盖。"""
    text = "輝立交易場公布\n強泰環保控股有限公司(1395)\n股份編號 ： 01395\n"
    with pytest.raises(ProviderError, match="文件内容为01395，疑源站文件覆盖"):
        parse_phillip_pdf_text(text, "01285", "2014-09-24")


PHILLIP_OCR_GAIN_TEXT = """
辉立证券（香港）有限公司
「辉立交易埸」公怖【香港訊一2020年07月14日】
祖龍娱樂有限公司
(股份代号：9990)
招股值
HKD 11.60
開市值格
HKD 13.00
最高值格（與招股值比较）
HKD 17.38
(49.83%)
最低值格（與招股值比较）
HKD 12.80
(10.34%)
最後收市值（與招股值比较）
HKD 17.20
(48.28%)
成交股敷
18,952,000股
9990
"""


def test_parse_phillip_pdf_ocr_text_recovers_gain_close():
    result = parse_phillip_pdf_ocr_text(
        PHILLIP_OCR_GAIN_TEXT, "09990", "2020-07-14", "https://example.com/09990.pdf"
    )
    assert result["grey_close"] == 17.2


def test_parse_phillip_pdf_ocr_text_rejects_gain_with_wrong_sign():
    with pytest.raises(ProviderError):
        parse_phillip_pdf_ocr_text(
            PHILLIP_OCR_GAIN_TEXT.replace("(48.28%)", "(-48.28%)"),
            "09990",
            "2020-07-14",
        )


PHILLIP_OCR_NARRATIVE_TEXT = """
輝立證券集團
辉立交易場公布
新天線色能源股份有限公司（00956）場外交易價
【香港訊—2010年10月12日】
經輝立交易场買賣之新天色能源的最高價HK$2.78及最低價HK$2.42。上市前收市價
HK$2.50，较定價HK$2.66低-6.02%。開市價HK$2.70，成交股數分别爲1,772,000股。
將於明天掛牌的新天綠色能源招股價定為每股 HK$2.66。
00956
"""


def test_parse_phillip_pdf_ocr_text_recovers_narrative_close():
    result = parse_phillip_pdf_ocr_text(
        PHILLIP_OCR_NARRATIVE_TEXT, "00956", "2010-10-12", "https://example.com/00956.pdf"
    )
    assert result["grey_close"] == 2.5


def test_parse_phillip_pdf_ocr_text_rejects_mismatched_narrative():
    with pytest.raises(ProviderError):
        parse_phillip_pdf_ocr_text(
            PHILLIP_OCR_NARRATIVE_TEXT.replace("低-6.02%", "低-5.02%"),
            "00956",
            "2010-10-12",
        )


def test_parse_phillip_pdf_falls_back_to_rendered_ocr(monkeypatch):
    class Page:
        @staticmethod
        def extract_text():
            return "㖗偅䨦 CID 乱码文本层"

    class Reader:
        pages = [Page()]

    monkeypatch.setattr("app.providers.PdfReader", lambda stream: Reader())
    monkeypatch.setattr(
        "app.providers._ocr_rendered_pdf_text", lambda content: PHILLIP_OCR_TEXT
    )
    result = parse_phillip_pdf(
        b"%PDF-fake", "06639", "2022-03-21", "https://example.com/06639.pdf"
    )
    assert result["grey_close"] == 14.36


def test_parse_phillip_pdf_keeps_original_error_when_ocr_fails(monkeypatch):
    class Page:
        @staticmethod
        def extract_text():
            return PHILLIP_TEXT

    class Reader:
        pages = [Page()]

    monkeypatch.setattr("app.providers.PdfReader", lambda stream: Reader())
    monkeypatch.setattr(
        "app.providers._ocr_rendered_pdf_text", lambda content: "无法核验的OCR文本"
    )
    with pytest.raises(ProviderError, match="股票代码与00020不匹配"):
        parse_phillip_pdf(b"%PDF-fake", "00020", "2021-12-29")


@pytest.mark.asyncio
async def test_phillip_provider_skips_weekend_and_missing_pdf(monkeypatch):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("20230106-02285C_Final.pdf"):
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=b"%PDF-fake")

    def fake_parse(content, code, grey_date, source_url, listing_date=None, issue_price=None):
        assert grey_date == "2023-01-05"
        assert listing_date == "2023-01-09"
        return {
            "grey_close": 50.25,
            "grey_platform": "辉立",
            "grey_source_url": source_url,
        }

    monkeypatch.setattr("app.providers.parse_phillip_pdf", fake_parse)
    async with mock_client(handler) as client:
        result = await PhillipGreyProvider(client).fetch_grey_market(
            "02285", "泉峰控股", "2023-01-09"
        )
    assert paths == [
        "/phillipMart/Files/20230106-02285C_Final.pdf",
        "/phillipMart/Files/20230105-02285C_Final.pdf",
    ]
    assert result["grey_status"] == "ok"
    assert result["grey_close"] == 50.25


class StaticGrey:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def fetch_grey_market(self, code, name, listing_date, issue_price=None):
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_combined_grey_provider_does_not_override_futu():
    primary = StaticGrey(
        {
            "grey_close": 51.15,
            "grey_platform": "富途",
            "grey_source_url": "https://example.com/futu",
            "grey_status": "ok",
            "grey_error": None,
        }
    )
    fallback = StaticGrey(
        {
            "grey_close": 50.25,
            "grey_platform": "辉立",
            "grey_source_url": "https://example.com/phillip",
            "grey_status": "ok",
            "grey_error": None,
        }
    )
    result = await GreyMarketProvider(primary, fallback).fetch_grey_market(
        "02285", "泉峰控股", "2021-12-30"
    )
    assert result["grey_close"] == 51.15
    assert result["grey_platform"] == "富途"
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_nested_grey_provider_prefers_gelonghui_futu_over_phillip():
    primary = StaticGrey(
        {
            "grey_close": None,
            "grey_platform": None,
            "grey_source_url": None,
            "grey_status": "not_found",
            "grey_error": None,
        }
    )
    gelonghui = StaticGrey(
        {
            "grey_close": 147.0,
            "grey_platform": "富途",
            "grey_source_url": "https://example.com/gelonghui",
            "grey_status": "ok",
            "grey_error": None,
        }
    )
    phillip = StaticGrey(
        {
            "grey_close": 146.0,
            "grey_platform": "辉立",
            "grey_source_url": "https://example.com/phillip",
            "grey_status": "ok",
            "grey_error": None,
        }
    )
    provider = GreyMarketProvider(
        primary, GreyMarketProvider(gelonghui, phillip)
    )
    result = await provider.fetch_grey_market("02670", "云迹", "2025-10-16")
    assert result["grey_close"] == 147.0
    assert result["grey_platform"] == "富途"
    assert phillip.calls == 0


@pytest.mark.asyncio
async def test_combined_grey_provider_uses_phillip_pdf_fallback():
    primary = StaticGrey(
        {
            "grey_close": None,
            "grey_platform": None,
            "grey_source_url": None,
            "grey_status": "not_found",
            "grey_error": None,
        }
    )
    fallback = StaticGrey(
        {
            "grey_close": 3.83,
            "grey_platform": "辉立",
            "grey_source_url": "https://example.com/phillip",
            "grey_status": "ok",
            "grey_error": None,
        }
    )
    result = await GreyMarketProvider(primary, fallback).fetch_grey_market(
        "00020", "商汤-W", "2021-12-30"
    )
    assert result["grey_status"] == "ok"
    assert result["grey_close"] == 3.83
    assert result["grey_platform"] == "辉立"


def hfq_row(day, close):
    return [day, close, close, close, close, "1", {}, "0", "1"]


@pytest.mark.asyncio
async def test_fetch_hfq_anchors_ignores_pre_listing_padding_bar():
    """腾讯后复权会给上市前区间补一根 2006-01-03 填充根，必须按上市日过滤。"""

    def handler(request):
        assert request.url.params["param"] == (
            "hk02128,day,2010-06-23,2026-11-09,640,hfq"
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "hk02128": {
                        "hfqday": [
                            hfq_row("2006-01-03", "0.580"),
                            hfq_row("2010-06-23", "2.440"),
                            hfq_row("2026-11-06", "6.610"),
                        ]
                    }
                },
            },
        )

    async with mock_client(handler) as client:
        anchors = await EastmoneyProvider(client).fetch_hfq_anchors(
            "02128", "2010-06-23", "2026-11-09"
        )
    assert anchors["hfq_first_close"] == 2.44
    assert anchors["hfq_first_date"] == "2010-06-23"
    assert anchors["hfq_last_close"] == 6.61
    assert anchors["hfq_last_date"] == "2026-11-06"


@pytest.mark.asyncio
async def test_fetch_hfq_anchors_refetches_early_window_for_old_stock():
    """老股单次请求只返回最后 640 根，需补取上市初期窗口，并同样过滤填充根。"""
    calls = []

    def handler(request):
        param = request.url.params["param"]
        calls.append(param)
        if param.endswith("2012-06-23,640,hfq"):
            rows = [
                hfq_row("2006-01-03", "0.580"),
                hfq_row("2010-06-23", "2.440"),
            ]
        else:
            rows = [hfq_row("2024-04-02", "6.250"), hfq_row("2026-11-06", "6.610")]
        return httpx.Response(
            200, json={"code": 0, "data": {"hk02128": {"hfqday": rows}}}
        )

    async with mock_client(handler) as client:
        anchors = await EastmoneyProvider(client).fetch_hfq_anchors(
            "02128", "2010-06-23", "2026-11-09"
        )
    assert len(calls) == 2
    assert anchors["hfq_first_close"] == 2.44
    assert anchors["hfq_last_close"] == 6.61


@pytest.mark.asyncio
async def test_fetch_hfq_anchors_rejects_when_only_padding_bar_exists():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"hk02128": {"hfqday": [hfq_row("2006-01-03", "0.580")]}},
            },
        )

    async with mock_client(handler) as client:
        with pytest.raises(ProviderError, match="上市初期"):
            await EastmoneyProvider(client).fetch_hfq_anchors(
                "02128", "2010-06-23", "2026-11-09"
            )
