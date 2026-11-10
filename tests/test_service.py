import asyncio
import tempfile
from datetime import date
from pathlib import Path

from app.service import (
    IPOService,
    adjusted_to_date_return,
    calculate_returns,
    data_window,
    hk_today,
    is_spac,
    listing_date_drift,
    percentage,
)


class FakeEastmoney:
    def __init__(
        self, listing_date, fail_list=False, fail_quotes=False,
        issue_way=None, issue_price=10.0, first_trade_date=None,
        hfq_first_close=11.0, hfq_last_close=20.0, fail_hfq=False,
    ):
        self.listing_date = listing_date
        self.fail_list = fail_list
        self.fail_quotes = fail_quotes
        self.issue_way = issue_way
        self.issue_price = issue_price
        self.first_trade_date = first_trade_date
        self.hfq_first_close = hfq_first_close
        self.hfq_last_close = hfq_last_close
        self.fail_hfq = fail_hfq

    async def fetch_ipo_list(self):
        if self.fail_list:
            raise RuntimeError("list unavailable")
        return [
            {"code": "09999", "name": "示例股份", "listing_date": self.listing_date},
            {"code": "00001", "name": "窗口外", "listing_date": "2009-01-02"},
        ]

    async def fetch_issue_info(self, code):
        return {
            "issue_price": self.issue_price,
            "listing_date": self.listing_date,
            "issue_way": self.issue_way,
        }

    async def fetch_price_history(self, code, listing_date):
        return {
            "first_close": 11.0,
            "first_trade_date": self.first_trade_date or listing_date,
            "historical_low": 8.0,
            "historical_high": 25.0,
        }

    async def fetch_latest_quotes(self, codes):
        if self.fail_quotes:
            raise RuntimeError("quotes unavailable")
        return {
            code: {
                "latest_price": 20.0,
                "quote_date": hk_today().isoformat(),
                "quote_time": "16:00:00",
            }
            for code in codes
        }

    async def fetch_hfq_anchors(self, code, listing_date, today):
        if self.fail_hfq:
            raise RuntimeError("hfq unavailable")
        return {
            "hfq_first_close": self.hfq_first_close,
            "hfq_first_date": listing_date,
            "hfq_last_close": self.hfq_last_close,
            "hfq_last_date": today,
        }


class FakeGrey:
    def __init__(self, found=True):
        self.found = found

    async def fetch_grey_market(self, code, name, listing_date, issue_price=None):
        if self.found:
            return {
                "grey_close": 12.0,
                "grey_platform": "富途",
                "grey_source_url": "https://example.com/grey",
                "grey_status": "ok",
                "grey_error": None,
            }
        return {
            "grey_close": None,
            "grey_platform": None,
            "grey_source_url": None,
            "grey_status": "not_found",
            "grey_error": None,
        }


def test_data_window_starts_from_2010():
    start, end = data_window(date(2024, 2, 29))
    assert start == date(2010, 1, 1)
    assert end == date(2024, 2, 29)


def test_return_calculations_and_missing_values():
    returns = calculate_returns(
        {"issue_price": 10, "grey_close": 12, "first_close": 11, "latest_price": 20}
    )
    assert returns == {
        "grey_return": 20.0,
        "first_day_return": 10.0,
        "to_date_return": 100.0,
    }
    assert percentage(None, 10) is None
    assert percentage(10, 0) is None


def test_refresh_persists_and_serializes_complete_record(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney(listing_date), FakeGrey(found=True)
    )
    asyncio.run(service.refresh())

    assert service.status["state"] == "success"
    result = service.get_ipos()
    assert result["total_all"] == 1
    item = result["items"][0]
    assert item["code"] == "09999"
    assert item["grey_return"] == 20.0
    assert item["first_day_return"] == 10.0
    assert item["to_date_return"] == 100.0
    assert item["historical_low"] == 8.0
    assert item["historical_high"] == 25.0
    assert item["grey_platform"] == "富途"


def test_not_found_grey_value_is_null_with_reason(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney(listing_date), FakeGrey(found=False)
    )
    asyncio.run(service.refresh())
    item = service.get_ipos()["items"][0]

    assert item["grey_close"] is None
    assert item["grey_return"] is None
    assert item["missing_reasons"]["grey"] == "AAStocks/辉立/格隆汇三源均无暗盘记录"
    assert item["to_date_return"] == 100.0


def test_despac_stock_is_excluded_from_grey_coverage(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney(listing_date), FakeGrey(found=False)
    )
    asyncio.run(service.refresh())

    with service._connect() as connection:
        connection.execute(
            "UPDATE ipos SET grey_applicable = 0, grey_close = NULL WHERE code = ?",
            ("09999",),
        )

    result = service.get_ipos()
    item = result["items"][0]
    assert item["grey_applicable"] == 0
    assert item["missing_reasons"]["grey"] == "De-SPAC合并上市，无公开招股暗盘交易"
    assert result["grey_stats"] == {"eligible": 0, "covered": 0, "inapplicable": 1}


def test_issue_note_is_migrated_and_exposed(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney(listing_date), FakeGrey(found=False)
    )
    asyncio.run(service.refresh())

    note = "De-SPAC 参考价：10.00 港元为 SPAC 信托单位赎回价，非公开发售定价"
    with service._connect() as connection:
        connection.execute(
            "UPDATE ipos SET issue_note = ? WHERE code = ?", (note, "09999")
        )

    item = service.get_ipos()["items"][0]
    assert item["issue_note"] == note


def test_inapplicable_note_overrides_despac_default(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney(listing_date), FakeGrey(found=False)
    )
    asyncio.run(service.refresh())

    note = "台风停市，富途/辉立暗盘未开市"
    with service._connect() as connection:
        connection.execute(
            "UPDATE ipos SET grey_applicable = 0, grey_note = ? WHERE code = ?",
            (note, "09999"),
        )

    item = service.get_ipos()["items"][0]
    assert item["missing_reasons"]["grey"] == note


def test_grey_note_takes_priority_over_generated_reason(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney(listing_date), FakeGrey(found=False)
    )
    asyncio.run(service.refresh())

    note = "AASTOCKS、格隆汇、辉立三源均无该股暗盘报道"
    with service._connect() as connection:
        connection.execute(
            "UPDATE ipos SET grey_note = ? WHERE code = ?", (note, "09999")
        )

    item = service.get_ipos()["items"][0]
    assert item["missing_reasons"]["grey"] == note


def test_introduction_listing_is_auto_marked_inapplicable(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db",
        FakeEastmoney(listing_date, issue_way="介绍上市", issue_price=None),
        FakeGrey(found=True),
    )
    asyncio.run(service.refresh())

    item = service.get_ipos()["items"][0]
    assert item["issue_way"] == "介绍上市"
    assert item["grey_applicable"] == 0
    assert item["grey_close"] is None
    assert item["issue_note"] == "介绍上市，无发行价（不涉及新股发行）"
    assert item["missing_reasons"]["grey"] == "介绍上市，无公开发售及暗盘交易"


def test_full_placing_listing_is_auto_marked_inapplicable(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db",
        FakeEastmoney(listing_date, issue_way="发售以供配售", issue_price=10.0),
        FakeGrey(found=True),
    )
    asyncio.run(service.refresh())

    item = service.get_ipos()["items"][0]
    assert item["grey_applicable"] == 0
    assert item["issue_note"] is None
    assert item["missing_reasons"]["grey"] == "全配售上市，无公开发售及散户暗盘交易"


def test_public_offer_listing_keeps_grey_applicable(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db",
        FakeEastmoney(listing_date, issue_way="发售以供认购,发售以供配售", issue_price=10.0),
        FakeGrey(found=True),
    )
    asyncio.run(service.refresh())

    item = service.get_ipos()["items"][0]
    assert item["grey_applicable"] == 1
    assert item["grey_close"] == 12.0
    assert item["grey_return"] == 20.0


def test_listing_date_drift_detects_reused_code():
    assert listing_date_drift("2026-05-05", "2016-10-17") > 365
    assert listing_date_drift("2026-05-04", "2026-05-05") == 1
    assert listing_date_drift(None, "2026-05-05") is None
    assert listing_date_drift("2026-05-05", None) is None
    assert listing_date_drift("bad-date", "2026-05-05") is None


def test_spac_identifier():
    assert is_spac("汇德收购-Z") is True
    assert is_spac("汇德收购-z") is True
    assert is_spac("龙资源") is False
    assert is_spac(None) is False


def test_spac_listing_drift_is_not_treated_as_reuse(tmp_path):
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney("2022-08-15"), FakeGrey(found=True)
    )
    service._upsert_listing(
        {"code": "07841", "name": "汇德收购-Z", "listing_date": "2022-08-15"}
    )
    service._update_fields("07841", grey_status="inapplicable")
    service._upsert_listing(
        {"code": "07841", "name": "汇德收购-Z", "listing_date": "2024-01-19"}
    )

    with service._connect() as connection:
        row = connection.execute(
            "SELECT listing_date, grey_status FROM ipos WHERE code = '07841'"
        ).fetchone()
    assert row["listing_date"] == "2024-01-19"
    assert row["grey_status"] == "inapplicable"
    assert not any("代码复用" in error for error in service.status["errors"])


def test_stale_listing_keeps_corrected_record(tmp_path):
    """历史 IPO 名单对复用代码仍返回旧实体，不应覆盖库里已校正的新实体记录。"""
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney("2026-05-05"), FakeGrey(found=True)
    )
    service._upsert_listing(
        {"code": "01609", "name": "天星医疗", "listing_date": "2026-05-05"}
    )
    service._update_fields(
        "01609",
        issue_price=98.5,
        first_close=215.0,
        grey_close=309.2,
        grey_status="ok",
        grey_platform="富途",
    )
    service._upsert_listing(
        {"code": "01609", "name": "创建集团控股", "listing_date": "2016-10-17"}
    )

    with service._connect() as connection:
        row = connection.execute(
            "SELECT * FROM ipos WHERE code = '01609'"
        ).fetchone()
        archived = connection.execute(
            "SELECT * FROM ipos_history WHERE code = '01609'"
        ).fetchone()
    assert row["name"] == "天星医疗"
    assert row["listing_date"] == "2026-05-05"
    assert row["issue_price"] == 98.5
    assert row["first_close"] == 215.0
    assert row["grey_close"] == 309.2
    assert row["grey_status"] == "ok"
    assert archived["name"] == "创建集团控股"
    assert archived["listing_date"] == "2016-10-17"


def test_first_trade_date_drift_corrects_listing_date(tmp_path):
    old_date = hk_today().replace(year=hk_today().year - 10).isoformat()
    new_date = hk_today().isoformat()
    service = IPOService(
        tmp_path / "ipo.db",
        FakeEastmoney(old_date, first_trade_date=new_date),
        FakeGrey(found=True),
    )
    asyncio.run(service.refresh())

    with service._connect() as connection:
        row = connection.execute(
            "SELECT listing_date, first_trade_date FROM ipos WHERE code = '09999'"
        ).fetchone()
    assert row["first_trade_date"] == new_date
    assert row["listing_date"] == new_date


def test_failed_forced_refresh_keeps_cached_rows(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney(listing_date), FakeGrey(found=True)
    )
    asyncio.run(service.refresh())
    service.eastmoney = FakeEastmoney(
        listing_date, fail_list=True, fail_quotes=True
    )

    asyncio.run(service.refresh(force=True))

    assert service.status["state"] == "partial"
    item = service.get_ipos()["items"][0]
    assert item["latest_price"] == 20.0
    assert item["to_date_return"] == 100.0


def test_filter_and_null_last_sorting(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney(listing_date), FakeGrey(found=False)
    )
    asyncio.run(service.refresh())

    assert service.get_ipos(query="不存在")["total"] == 0
    assert service.get_ipos(query="示例")["total"] == 1
    assert service.get_ipos(year=listing_date[:4])["total"] == 1
    assert service.get_ipos(sort="grey_return", order="desc")["items"][0][
        "grey_return"
    ] is None


def test_database_connections_allow_temporary_directory_cleanup():
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    with tempfile.TemporaryDirectory() as directory:
        service = IPOService(
            Path(directory) / "ipo.db", FakeEastmoney(listing_date), FakeGrey()
        )
        asyncio.run(service.refresh())
        assert service.get_ipos()["total"] == 1


def test_grey_market_requests_are_serialized(tmp_path):
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()

    class MultiEastmoney(FakeEastmoney):
        async def fetch_ipo_list(self):
            return [
                {
                    "code": f"1000{index}",
                    "name": f"示例{index}",
                    "listing_date": listing_date,
                }
                for index in range(3)
            ]

    class TrackingGrey(FakeGrey):
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def fetch_grey_market(self, code, name, listing_date, issue_price=None):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return await super().fetch_grey_market(code, name, listing_date)

    grey = TrackingGrey()
    service = IPOService(tmp_path / "ipo.db", MultiEastmoney(listing_date), grey)
    asyncio.run(service.refresh())

    assert service.get_ipos()["total_all"] == 3
    assert grey.max_active == 1


def test_refresh_skips_delisted_records(tmp_path):
    """已退市标的行情源不再返回数据，刷新时应跳过请求并保留历史数据。"""
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()

    class TrackingEastmoney(FakeEastmoney):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.quote_codes = None

        async def fetch_latest_quotes(self, codes):
            self.quote_codes = list(codes)
            return await super().fetch_latest_quotes(codes)

    eastmoney = TrackingEastmoney(listing_date)
    service = IPOService(tmp_path / "ipo.db", eastmoney, FakeGrey(found=True))
    asyncio.run(service.refresh())
    service._update_fields("09999", market_status="delisted")
    eastmoney.quote_codes = None

    asyncio.run(service.refresh(force=True))

    assert eastmoney.quote_codes is None
    item = service.get_ipos()["items"][0]
    assert item["market_status"] == "delisted"
    assert item["latest_price"] == 20.0


def test_suspended_quotes_use_longer_cache_ttl(tmp_path):
    """停牌标的在 30 天缓存期内不应重复请求行情。"""
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()

    class TrackingEastmoney(FakeEastmoney):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.quote_calls = 0

        async def fetch_latest_quotes(self, codes):
            self.quote_calls += 1
            return await super().fetch_latest_quotes(codes)

    eastmoney = TrackingEastmoney(listing_date)
    service = IPOService(tmp_path / "ipo.db", eastmoney, FakeGrey(found=True))
    asyncio.run(service.refresh())
    service._update_fields("09999", market_status="suspended")

    asyncio.run(service.refresh())

    assert eastmoney.quote_calls == 1
    item = service.get_ipos()["items"][0]
    assert item["market_status"] == "suspended"
    assert item["latest_price"] == 20.0


def test_legacy_single_key_schema_is_migrated_to_composite_pk(tmp_path):
    """旧库的 code 单主键应自动迁移为 (code, listing_date) 复合主键，数据不丢。"""
    import sqlite3

    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE ipos (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            listing_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO ipos VALUES ('01609', '创建集团控股', '2016-10-17', 'x', 'x')"
    )
    connection.commit()
    connection.close()

    service = IPOService(db, FakeEastmoney("2026-05-05"), FakeGrey(found=True))

    with service._connect() as conn:
        primary_keys = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(ipos)")
        }
        row = conn.execute("SELECT * FROM ipos WHERE code = '01609'").fetchone()
    assert primary_keys["code"] == 1
    assert primary_keys["listing_date"] == 2
    assert row["name"] == "创建集团控股"
    assert row["grey_applicable"] == 1


def test_composite_key_allows_reused_code_with_two_entities(tmp_path):
    """复合主键应允许同一代码的新旧实体并存。"""
    service = IPOService(
        tmp_path / "ipo.db", FakeEastmoney("2026-05-05"), FakeGrey(found=True)
    )
    with service._connect() as connection:
        for name, listing_date, issue_price in [
            ("创建集团控股", "2016-10-17", 1.5),
            ("天星医疗", "2026-05-05", 98.5),
        ]:
            connection.execute(
                """
                INSERT INTO ipos
                    (code, name, listing_date, issue_price, created_at, updated_at)
                VALUES ('01609', ?, ?, ?, 'x', 'x')
                """,
                (name, listing_date, issue_price),
            )

    rows = service.get_ipos()["items"]
    assert len(rows) == 2
    assert [row["listing_date"] for row in rows] == ["2026-05-05", "2016-10-17"]
    # 详情页取最新实体
    assert service.get_stock("01609")["name"] == "天星医疗"


def test_adjusted_to_date_return_formula():
    # 无送转：复权收益与现价口径一致，因子为 1
    ret, factor = adjusted_to_date_return(10.0, 11.0, 20.0, 11.0, 20.0)
    assert ret == 100.0
    assert factor == 1.0
    # 10送10（因子2）：复权收益应还原摊薄，高于现价口径
    ret, factor = adjusted_to_date_return(10.0, 11.0, 20.0, 11.0, 40.0)
    assert ret == 300.0
    assert factor == 2.0
    # 任一输入缺失或非正数时返回空
    assert adjusted_to_date_return(None, 11.0, 20.0, 11.0, 20.0) == (None, None)
    assert adjusted_to_date_return(10.0, 0, 20.0, 11.0, 20.0) == (None, None)
    assert adjusted_to_date_return(10.0, 11.0, 20.0, 11.0, None) == (None, None)


def test_refresh_computes_adjusted_return_with_bonus_factor(tmp_path):
    """发生过送转的标的，复权收益与因子应写库并随详情返回。"""
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db",
        FakeEastmoney(listing_date, hfq_first_close=11.0, hfq_last_close=40.0),
        FakeGrey(found=True),
    )
    asyncio.run(service.refresh())

    assert service.status["state"] == "success"
    item = service.get_ipos()["items"][0]
    assert item["to_date_return"] == 100.0
    assert item["to_date_return_adj"] == 300.0
    assert item["adj_factor"] == 2.0
    assert item["adj_updated_at"]


def test_hfq_failure_records_error_without_breaking_refresh(tmp_path):
    """后复权数据失败不应拖垮刷新，仅记录错误并置空复权字段。"""
    listing_date = hk_today().replace(year=hk_today().year - 1).isoformat()
    service = IPOService(
        tmp_path / "ipo.db",
        FakeEastmoney(listing_date, fail_hfq=True),
        FakeGrey(found=True),
    )
    asyncio.run(service.refresh())

    # 复权失败只记错误，不影响其余字段落库
    assert service.status["state"] == "partial"
    item = service.get_ipos()["items"][0]
    assert item["to_date_return"] == 100.0
    assert item["to_date_return_adj"] is None
    assert item["adj_factor"] is None
    assert item["adj_updated_at"]
    assert any("复权数据" in error for error in service.status["errors"])
