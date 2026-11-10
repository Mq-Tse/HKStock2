from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .providers import EastmoneyProvider, GreyMarketProvider, ProviderError

HK_TIMEZONE = timezone(timedelta(hours=8))
UTC = timezone.utc
LIST_CACHE_TTL = timedelta(hours=6)
QUOTE_CACHE_TTL = timedelta(minutes=15)
# 停牌标的行情源长期不返回新成交价，放宽重试间隔避免每次刷新都发起无效请求。
SUSPENDED_QUOTE_CACHE_TTL = timedelta(days=30)
GREY_RETRY_TTL = timedelta(hours=24)
HISTORY_CACHE_TTL = timedelta(hours=24)
# 同一代码的上市日跳变超过该天数，判定为港股代码被其他实体复用
LISTING_DATE_DRIFT_DAYS = 365

# 复合主键 (code, listing_date)：允许同一代码在旧实体退市、代码被复用后
# 同时保留新旧两个实体，避免旧记录被迫覆盖或丢弃。
IPOS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ipos (
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    listing_date TEXT NOT NULL,
    issue_price REAL,
    issue_error TEXT,
    first_close REAL,
    first_trade_date TEXT,
    first_error TEXT,
    historical_low REAL,
    historical_high REAL,
    history_updated_at TEXT,
    grey_close REAL,
    grey_platform TEXT,
    grey_source_url TEXT,
    grey_status TEXT,
    grey_error TEXT,
    grey_attempted_at TEXT,
    grey_applicable INTEGER NOT NULL DEFAULT 1,
    grey_note TEXT,
    grey_retry_disabled INTEGER NOT NULL DEFAULT 0,
    issue_note TEXT,
    issue_way TEXT,
    latest_price REAL,
    quote_date TEXT,
    quote_time TEXT,
    quote_error TEXT,
    quotes_updated_at TEXT,
    market_status TEXT,
    last_trade_date TEXT,
    to_date_return_adj REAL,
    adj_factor REAL,
    adj_updated_at TEXT,
    details_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (code, listing_date)
)
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def hk_today() -> date:
    return datetime.now(HK_TIMEZONE).date()


DATA_START = date(2010, 1, 1)


def data_window(today: date | None = None) -> tuple[date, date]:
    end = today or hk_today()
    return DATA_START, end


def percentage(value: float | None, base: float | None) -> float | None:
    if value is None or base is None or base <= 0:
        return None
    return round((value / base - 1) * 100, 2)


def calculate_returns(record: dict[str, Any]) -> dict[str, float | None]:
    issue_price = record.get("issue_price")
    return {
        "grey_return": percentage(record.get("grey_close"), issue_price),
        "first_day_return": percentage(record.get("first_close"), issue_price),
        "to_date_return": percentage(record.get("latest_price"), issue_price),
    }


def adjusted_to_date_return(
    issue_price: float | None,
    first_close: float | None,
    latest_price: float | None,
    hfq_first_close: float | None,
    hfq_last_close: float | None,
) -> tuple[float | None, float | None]:
    """复权至今收益（%）与送转调整因子。

    后复权K线以首日价格为锚，hfq_last/hfq_first 即剔除送转后的持有回报；
    再乘上 first_close/issue_price 还原从发行价起的完整收益链。
    因子 = 复权回报 / 现价回报，|因子-1| 大说明发生过送转/拆股。
    供股/配股的摊薄不在后复权中体现，属有意排除。
    """
    values = (issue_price, first_close, latest_price, hfq_first_close, hfq_last_close)
    if any(v is None or v <= 0 for v in values):
        return None, None
    factor = (hfq_last_close / hfq_first_close) / (latest_price / first_close)
    adjusted = ((hfq_last_close / hfq_first_close) * (first_close / issue_price) - 1) * 100
    return round(adjusted, 2), round(factor, 4)


ISSUE_NOTE_INTRO = "介绍上市，无发行价（不涉及新股发行）"
GREY_NOTE_INTRO = "介绍上市，无公开发售及暗盘交易"
GREY_NOTE_PLACING = "全配售上市，无公开发售及散户暗盘交易"


def grey_inapplicable_note(issue_way: str | None) -> str | None:
    """按东财F10发行方式判断该新股是否不设散户暗盘，返回原因说明。"""
    if not issue_way:
        return None
    if "介绍上市" in issue_way:
        return GREY_NOTE_INTRO
    if "发售以供认购" not in issue_way and "发售以供配售" in issue_way:
        return GREY_NOTE_PLACING
    return None


def listing_date_drift(current: str | None, previous: str | None) -> int | None:
    """返回两个上市日的天数差绝对值；任一侧缺失或非法时返回 None。"""
    if not current or not previous:
        return None
    try:
        return abs(
            (date.fromisoformat(current) - date.fromisoformat(previous)).days
        )
    except ValueError:
        return None


def is_spac(name: str | None) -> bool:
    """SPAC 股份在名称末尾带 -Z 标记，其上市日与并购完成后交易日天然不同。"""
    return bool(name) and name.strip().upper().endswith("-Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC)
    except (TypeError, ValueError):
        return None


def _is_fresh(value: str | None, ttl: timedelta) -> bool:
    parsed = _parse_time(value)
    return bool(parsed and utc_now() - parsed < ttl)


class IPOService:
    def __init__(
        self,
        db_path: str | Path,
        eastmoney: EastmoneyProvider,
        grey_provider: GreyMarketProvider,
    ) -> None:
        self.db_path = Path(db_path)
        self.eastmoney = eastmoney
        self.grey_provider = grey_provider
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._status: dict[str, Any] = {
            "state": "idle",
            "running": False,
            "message": "等待刷新",
            "completed": 0,
            "total": 0,
            "current": None,
            "started_at": None,
            "finished_at": None,
            "error_count": 0,
            "errors": [],
        }
        self.init_db()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(IPOS_TABLE_SQL)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(ipos)")
            }
            for name in ("historical_low", "historical_high"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE ipos ADD COLUMN {name} REAL"
                    )
            if "history_updated_at" not in columns:
                connection.execute(
                    "ALTER TABLE ipos ADD COLUMN history_updated_at TEXT"
                )
            if "grey_applicable" not in columns:
                connection.execute(
                    "ALTER TABLE ipos ADD COLUMN grey_applicable INTEGER NOT NULL DEFAULT 1"
                )
            if "grey_note" not in columns:
                connection.execute("ALTER TABLE ipos ADD COLUMN grey_note TEXT")
            if "issue_note" not in columns:
                connection.execute("ALTER TABLE ipos ADD COLUMN issue_note TEXT")
            if "issue_way" not in columns:
                connection.execute("ALTER TABLE ipos ADD COLUMN issue_way TEXT")
            if "market_status" not in columns:
                connection.execute("ALTER TABLE ipos ADD COLUMN market_status TEXT")
            if "last_trade_date" not in columns:
                connection.execute("ALTER TABLE ipos ADD COLUMN last_trade_date TEXT")
            if "grey_retry_disabled" not in columns:
                connection.execute(
                    "ALTER TABLE ipos ADD COLUMN grey_retry_disabled INTEGER NOT NULL DEFAULT 0"
                )
            for adj_column in ("to_date_return_adj", "adj_factor"):
                if adj_column not in columns:
                    connection.execute(
                        f"ALTER TABLE ipos ADD COLUMN {adj_column} REAL"
                    )
            if "adj_updated_at" not in columns:
                connection.execute(
                    "ALTER TABLE ipos ADD COLUMN adj_updated_at TEXT"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
            )
            # 记录代码复用前旧实体的快照，便于日后追溯被替换的历史新股。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ipos_history (
                    code TEXT NOT NULL,
                    name TEXT,
                    listing_date TEXT,
                    issue_price REAL,
                    first_close REAL,
                    grey_close REAL,
                    grey_platform TEXT,
                    archived_at TEXT NOT NULL
                )
                """
            )
            self._migrate_composite_pk(connection)

    def _migrate_composite_pk(self, connection: sqlite3.Connection) -> None:
        """把旧库的 code 单主键迁移为 (code, listing_date) 复合主键。"""
        primary_keys = {
            row["name"]: row["pk"]
            for row in connection.execute("PRAGMA table_info(ipos)")
        }
        if primary_keys.get("listing_date", 0) > 0:
            return
        columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(ipos)")
        ]
        connection.execute("ALTER TABLE ipos RENAME TO ipos_legacy")
        connection.execute(IPOS_TABLE_SQL)
        column_list = ", ".join(columns)
        connection.execute(
            f"INSERT INTO ipos ({column_list}) SELECT {column_list} FROM ipos_legacy"
        )
        connection.execute("DROP TABLE ipos_legacy")

    @property
    def status(self) -> dict[str, Any]:
        status = dict(self._status)
        status["errors"] = list(self._status["errors"])
        if self._refresh_task and not self._refresh_task.done():
            status["running"] = True
            status["state"] = "running"
        return status

    def start_refresh(self, force: bool = False) -> bool:
        if self._refresh_task and not self._refresh_task.done():
            return False
        self._refresh_task = asyncio.create_task(self.refresh(force=force))
        return True

    async def shutdown(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    def _get_meta(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def _upsert_listing(self, item: dict[str, Any]) -> None:
        now = iso_now()
        listing_date = item.get("listing_date")
        if not listing_date:
            return
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT name, listing_date FROM ipos
                WHERE code = ? ORDER BY listing_date DESC LIMIT 1
                """,
                (item["code"],),
            ).fetchone()
            drift = listing_date_drift(
                listing_date,
                existing["listing_date"] if existing else None,
            )
            if (
                drift is not None
                and drift > LISTING_DATE_DRIFT_DAYS
                and not is_spac(existing["name"])
            ):
                # 新股名单是按历史 IPO 记录返回的，同一代码被新实体复用后
                # 名单里仍是旧实体的名称与上市日。此时保留库里已校正的记录，
                # 由详情阶段的行情校验（首日交易日）决定最终上市日。
                connection.execute(
                    """
                    INSERT INTO ipos_history (code, name, listing_date, archived_at)
                    SELECT ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM ipos_history
                        WHERE code = ? AND name = ? AND listing_date = ?
                    )
                    """,
                    (
                        item["code"],
                        item.get("name"),
                        listing_date,
                        now,
                        item["code"],
                        item.get("name"),
                        listing_date,
                    ),
                )
                connection.execute(
                    "UPDATE ipos SET updated_at = ? WHERE code = ? AND listing_date = ?",
                    (now, item["code"], existing["listing_date"]),
                )
                return
            if existing:
                # 同一实体的上市日小幅修正（≤365 天）就地更新，避免复合主键产生重复行。
                connection.execute(
                    """
                    UPDATE ipos SET name = ?, listing_date = ?, updated_at = ?
                    WHERE code = ? AND listing_date = ?
                    """,
                    (
                        item.get("name") or item["code"],
                        listing_date,
                        now,
                        item["code"],
                        existing["listing_date"],
                    ),
                )
                return
            connection.execute(
                """
                INSERT INTO ipos (code, name, listing_date, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    item["code"],
                    item.get("name") or item["code"],
                    listing_date,
                    now,
                    now,
                ),
            )

    def _update_fields(
        self, code: str, listing_key: str | None = None, **fields: Any
    ) -> None:
        if not fields:
            return
        fields["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        if listing_key is None:
            values = list(fields.values()) + [code]
            where = "code = ?"
        else:
            values = list(fields.values()) + [code, listing_key]
            where = "code = ? AND listing_date = ?"
        with self._connect() as connection:
            connection.execute(
                f"UPDATE ipos SET {assignments} WHERE {where}", values
            )

    def _records(self, start: date, end: date) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ipos
                WHERE listing_date IS NOT NULL
                  AND listing_date >= ? AND listing_date <= ?
                ORDER BY listing_date DESC, code DESC
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    async def refresh(self, force: bool = False) -> None:
        async with self._refresh_lock:
            started_at = iso_now()
            self._status = {
                "state": "running",
                "running": True,
                "message": "正在获取新股列表",
                "completed": 0,
                "total": 0,
                "current": None,
                "started_at": started_at,
                "finished_at": None,
                "error_count": 0,
                "errors": [],
            }
            try:
                await self._do_refresh(force)
            except asyncio.CancelledError:
                self._status.update(
                    state="idle", running=False, message="刷新已停止", finished_at=iso_now()
                )
                raise
            except Exception as exc:
                self._status.update(
                    state="error",
                    running=False,
                    message=f"刷新失败：{exc}",
                    finished_at=iso_now(),
                    error_count=self._status["error_count"] + 1,
                )
                self._status["errors"] = (self._status["errors"] + [str(exc)])[-20:]

    def _record_error(self, code: str, stage: str, exc: Exception) -> None:
        message = f"{code} {stage}：{exc}"
        self._status["error_count"] += 1
        self._status["errors"] = (self._status["errors"] + [message])[-20:]

    async def _do_refresh(self, force: bool) -> None:
        start, end = data_window()
        records = self._records(start, end)
        list_fresh = _is_fresh(self._get_meta("ipo_list_updated_at"), LIST_CACHE_TTL)

        if force or not list_fresh or not records:
            try:
                listings = await self.eastmoney.fetch_ipo_list()
                for item in listings:
                    listing_date = item.get("listing_date")
                    if listing_date and start.isoformat() <= listing_date <= end.isoformat():
                        self._upsert_listing(item)
                self._set_meta("ipo_list_updated_at", iso_now())
            except Exception as exc:
                self._record_error("IPO", "列表获取失败", exc)
                if not records:
                    raise RuntimeError(f"新股列表不可用：{exc}") from exc
            records = self._records(start, end)

        self._status.update(total=len(records), message="正在更新行情与新股资料")
        # 已退市标的行情源不再返回数据，跳过请求可避免每次刷新产生大量无效错误。
        # 停牌标的按较长的缓存周期重试，避免每次刷新都触发无效请求。
        codes = [
            record["code"]
            for record in records
            if record.get("market_status") != "delisted"
            and (
                force
                or not _is_fresh(
                    record.get("quotes_updated_at"),
                    SUSPENDED_QUOTE_CACHE_TTL
                    if record.get("market_status") == "suspended"
                    else QUOTE_CACHE_TTL,
                )
            )
        ]
        if codes:
            try:
                quotes = await self.eastmoney.fetch_latest_quotes(
                    sorted(set(codes))
                )
                requested = set(codes)
                for record in records:
                    code = record["code"]
                    if code not in requested:
                        continue
                    quote = quotes.get(code)
                    if quote:
                        # 行情源对已退市/停牌股票不返回成交价，需给出明确原因，
                        # 避免与“数据缺失”混淆。
                        self._update_fields(
                            code,
                            record.get("listing_date"),
                            latest_price=quote.get("latest_price"),
                            quote_date=quote.get("quote_date"),
                            quote_time=quote.get("quote_time"),
                            quote_error=(
                                None
                                if quote.get("latest_price") is not None
                                else "无最新成交价（已退市、停牌或尚未上市）"
                            ),
                            quotes_updated_at=iso_now(),
                        )
                    else:
                        self._update_fields(
                            code,
                            record.get("listing_date"),
                            quote_error="未获取最新行情",
                        )
            except Exception as exc:
                self._record_error("HK", "最新行情获取失败", exc)

        records = self._records(start, end)
        semaphore = asyncio.Semaphore(4)
        grey_semaphore = asyncio.Semaphore(1)

        async def update_record(record: dict[str, Any]) -> None:
            async with semaphore:
                code = record["code"]
                self._status["current"] = code
                # 已退市标的行情源不再返回最新价，批量行情抓取阶段已自动跳过；
                # 但发行资料、历史日 K、暗盘、复权等对退市股仍有价值，这里继续补抓。
                updates: dict[str, Any] = {}
                try:
                    # 已人工标注 issue_note 的记录（介绍上市、De-SPAC参考价）不再回源，
                    # 避免数据源无发行价时每次刷新都重复报错。
                    # 暗盘尚未取到且未缓存发行方式的记录也回源一次，用于自动判定是否设暗盘。
                    if (
                        (record.get("issue_price") is None and not record.get("issue_note"))
                        or not record.get("listing_date")
                        or (
                            not record.get("issue_way")
                            and record.get("grey_applicable", 1)
                            and record.get("grey_status") != "ok"
                        )
                    ):
                        try:
                            issue = await self.eastmoney.fetch_issue_info(code)
                            updates.update(
                                issue_price=issue.get("issue_price"),
                                listing_date=issue.get("listing_date")
                                or record.get("listing_date"),
                                issue_error=None,
                                issue_way=issue.get("issue_way"),
                            )
                            inapplicable_note = grey_inapplicable_note(
                                issue.get("issue_way")
                            )
                            if inapplicable_note:
                                updates["grey_applicable"] = 0
                                updates["grey_status"] = "inapplicable"
                                updates["grey_note"] = inapplicable_note
                                # 介绍上市/全配售不应残留之前抓取的暗盘数据
                                updates["grey_close"] = None
                                updates["grey_platform"] = None
                                updates["grey_source_url"] = None
                                updates["grey_error"] = None
                                if "介绍上市" in (issue.get("issue_way") or ""):
                                    updates["issue_note"] = ISSUE_NOTE_INTRO
                            record.update(updates)
                            if issue.get("issue_price") is None:
                                updates["issue_error"] = "数据源未提供最终发行价"
                        except Exception as exc:
                            updates["issue_error"] = str(exc)
                            self._record_error(code, "发行资料", exc)

                    listing_date = updates.get("listing_date") or record.get("listing_date")
                    history_stale = not _is_fresh(
                        record.get("history_updated_at"), HISTORY_CACHE_TTL
                    )
                    if (
                        listing_date
                        and (force or record.get("first_close") is None or history_stale)
                    ):
                        try:
                            history = await self.eastmoney.fetch_price_history(
                                code, listing_date
                            )
                            updates.update(
                                first_close=history.get("first_close"),
                                first_trade_date=history.get("first_trade_date"),
                                first_error=None,
                                historical_low=history.get("historical_low"),
                                historical_high=history.get("historical_high"),
                                history_updated_at=iso_now(),
                            )
                            drift = listing_date_drift(
                                history.get("first_trade_date"), listing_date
                            )
                            if (
                                drift is not None
                                and drift > LISTING_DATE_DRIFT_DAYS
                                and not is_spac(record.get("name"))
                            ):
                                # 首日交易日与记录上市日相差过大：代码已被新实体复用，
                                # 以行情源的首日交易日为准修正上市日。
                                updates["listing_date"] = history.get(
                                    "first_trade_date"
                                )
                                self._record_error(
                                    code, "代码复用检测",
                                    ProviderError(
                                        f"上市日 {listing_date} 与首日交易日 "
                                        f"{history.get('first_trade_date')} 相差"
                                        f"{drift}天，疑代码复用，已按首日交易日修正"
                                    ),
                                )
                        except Exception as exc:
                            updates["first_error"] = str(exc)
                            self._record_error(code, "首日及历史行情", exc)

                    # 复权至今收益：后复权日K剥离送转/拆股影响（供股摊薄有意排除）。
                    adj_stale = not _is_fresh(
                        record.get("adj_updated_at"), HISTORY_CACHE_TTL
                    )
                    adj_issue = updates.get("issue_price", record.get("issue_price"))
                    adj_first = updates.get("first_close", record.get("first_close"))
                    adj_latest = record.get("latest_price")
                    if (
                        listing_date
                        and adj_issue
                        and adj_first
                        and adj_latest
                        and (force or adj_stale)
                    ):
                        try:
                            anchors = await self.eastmoney.fetch_hfq_anchors(
                                code, listing_date, hk_today().isoformat()
                            )
                            ret_adj, factor = adjusted_to_date_return(
                                adj_issue,
                                adj_first,
                                adj_latest,
                                anchors["hfq_first_close"],
                                anchors["hfq_last_close"],
                            )
                            updates.update(
                                to_date_return_adj=ret_adj,
                                adj_factor=factor,
                                adj_updated_at=iso_now(),
                            )
                        except Exception as exc:
                            # 失败也写时间戳，24小时内不重复请求。
                            updates["adj_updated_at"] = iso_now()
                            self._record_error(code, "复权数据", exc)

                    grey_fresh = _is_fresh(
                        record.get("grey_attempted_at"), GREY_RETRY_TTL
                    )
                    if (
                        record.get("grey_applicable", 1)
                        and record.get("grey_status") != "ok"
                        and not record.get("grey_retry_disabled")
                        and listing_date
                        and (force or not grey_fresh)
                    ):
                        try:
                            # AASTOCKS并发检索易返回500，暗盘请求单独串行。
                            async with grey_semaphore:
                                grey = await self.grey_provider.fetch_grey_market(
                                    code,
                                    record.get("name") or code,
                                    listing_date,
                                    issue_price=record.get("issue_price"),
                                )
                            grey_status = grey.get("grey_status", "not_found")
                            # 暗盘收盘价护栏：> 1000 港元不可能，典型是 PDF 解析抓成了股数/成交额
                            grey_close = grey.get("grey_close")
                            if grey_close and grey_close > 1000:
                                grey_close = None
                                grey_error = "暗盘收盘价疑似单位错误（>1000），已清除"
                                grey_status = "not_found"
                            else:
                                grey_error = grey.get("grey_error") or (
                                    "AAStocks/辉立/格隆汇三源均无暗盘记录"
                                    if grey_status == "not_found"
                                    else None
                                )
                            updates.update(
                                grey_close=grey_close,
                                grey_platform=grey.get("grey_platform"),
                                grey_source_url=grey.get("grey_source_url"),
                                grey_status=grey_status,
                                grey_error=grey_error,
                                grey_attempted_at=iso_now(),
                            )
                        except Exception as exc:
                            updates.update(
                                grey_status="error",
                                grey_error=str(exc),
                                grey_attempted_at=iso_now(),
                            )
                            self._record_error(code, "暗盘资料", exc)

                    updates["details_updated_at"] = iso_now()
                    self._update_fields(
                        code, record.get("listing_date"), **updates
                    )
                finally:
                    self._status["completed"] += 1

        await asyncio.gather(*(update_record(record) for record in records))
        self._set_meta("last_refresh_at", iso_now())
        error_count = self._status["error_count"]
        self._status.update(
            state="partial" if error_count else "success",
            running=False,
            message=(
                f"刷新完成，{error_count} 项数据暂未获取"
                if error_count
                else "刷新完成"
            ),
            current=None,
            finished_at=iso_now(),
        )

    def _missing_reasons(self, record: dict[str, Any]) -> dict[str, str | None]:
        issue_missing = record.get("issue_price") is None
        grey_inapplicable = not record.get("grey_applicable", 1)
        issue_reason = (
            record.get("issue_note")
            or record.get("issue_error")
            or "缺少最终发行价"
        )
        return {
            "grey": (
                record.get("grey_note")
                or "De-SPAC合并上市，无公开招股暗盘交易"
                if grey_inapplicable
                else (
                    issue_reason
                    if issue_missing
                    else (
                        record.get("grey_note")
                        or record.get("grey_error")
                        or (
                            "未获取富途或辉立暗盘收盘数据"
                            if record.get("grey_close") is None
                            else None
                        )
                    )
                )
            ),
            "first_day": (
                issue_reason
                if issue_missing
                else record.get("first_error")
                or ("未获取上市首日收盘价" if record.get("first_close") is None else None)
            ),
            "to_date": (
                issue_reason
                if issue_missing
                else record.get("quote_error")
                or ("未获取最新行情" if record.get("latest_price") is None else None)
            ),
            "history": (
                record.get("first_error")
                or (
                    "未获取上市以来历史价格"
                    if record.get("historical_low") is None
                    or record.get("historical_high") is None
                    else None
                )
            ),
        }

    def get_ipos(
        self,
        query: str = "",
        year: str | None = None,
        sort: str = "listing_date",
        order: str = "desc",
    ) -> dict[str, Any]:
        start, end = data_window()
        records = self._records(start, end)
        overall_total = len(records)
        normalized_query = query.strip().lower()
        if normalized_query:
            records = [
                record
                for record in records
                if normalized_query in record["code"].lower()
                or normalized_query in (record.get("name") or "").lower()
            ]
        if year and year.isdigit():
            records = [
                record
                for record in records
                if (record.get("listing_date") or "").startswith(year)
            ]

        items = []
        for record in records:
            item = dict(record)
            item.update(calculate_returns(record))
            item["missing_reasons"] = self._missing_reasons(record)
            items.append(item)

        allowed_sorts = {
            "code",
            "name",
            "listing_date",
            "issue_price",
            "grey_return",
            "first_day_return",
            "latest_price",
            "historical_low",
            "historical_high",
            "to_date_return",
        }
        sort = sort if sort in allowed_sorts else "listing_date"
        present = [item for item in items if item.get(sort) is not None]
        missing = [item for item in items if item.get(sort) is None]
        present.sort(key=lambda item: item[sort], reverse=order.lower() != "asc")
        items = present + missing
        years = sorted(
            {
                int(record["listing_date"][:4])
                for record in self._records(start, end)
                if record.get("listing_date")
            },
            reverse=True,
        )
        # 暗盘覆盖率只统计适用口径的记录（De-SPAC 无暗盘交易，不计入分母）。
        grey_eligible = [
            item for item in items if item.get("grey_applicable", 1)
        ]
        grey_covered = [
            item for item in grey_eligible if item.get("grey_close") is not None
        ]
        return {
            "items": items,
            "total": len(items),
            "total_all": overall_total,
            "grey_stats": {
                "eligible": len(grey_eligible),
                "covered": len(grey_covered),
                "inapplicable": len(items) - len(grey_eligible),
            },
            "years": years,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "last_updated": self._get_meta("last_refresh_at"),
            "status": self.status,
        }

    def get_stock(self, code: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM ipos WHERE code = ?
                ORDER BY listing_date DESC LIMIT 1
                """,
                (code,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item.update(calculate_returns(item))
        item["missing_reasons"] = self._missing_reasons(item)
        return item
