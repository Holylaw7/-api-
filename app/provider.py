"""Official HiThink REST adapter. Public methods return the envelope's data.

Only stdlib is used. No upstream response bodies, headers or credentials appear
in exceptions. Pagination failures are errors rather than successful empty data.
"""
from __future__ import annotations

import hashlib
import copy
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SHANGHAI = timezone(timedelta(hours=8))
BASE_URL = "https://fuyao.aicubes.cn"


class _SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.netloc != "fuyao.aicubes.cn":
            raise APIError("拒绝向不同域名或非 HTTPS 地址转发认证请求")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def urlopen(request, timeout):
    """Keep the credential on the documented TLS origin even after redirects."""
    return build_opener(_SameOriginRedirect()).open(request, timeout=timeout)


class APIError(Exception):
    """Safe to display; deliberately excludes the server's arbitrary message."""

    def __init__(self, message="数据请求失败", *, code=None, request_id=None,
                 retry_after_seconds=None):
        self.code = code
        self.retry_after_seconds = (float(retry_after_seconds)
                                    if isinstance(retry_after_seconds, (int, float))
                                    and not isinstance(retry_after_seconds, bool)
                                    and math.isfinite(retry_after_seconds)
                                    and retry_after_seconds >= 0 else None)
        self.request_id = (request_id if isinstance(request_id, str)
                           and "sk-" not in request_id.lower()
                           and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", request_id) else None)
        suffix = f"（code={code}）" if isinstance(code, int) else ""
        if self.request_id:
            suffix += f" [request_id={self.request_id}]"
        super().__init__(message + suffix)


def date_ms(value: str) -> int:
    """ISO date to Asia/Shanghai midnight milliseconds."""
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=SHANGHAI).timestamp() * 1000)


def iso_date(timestamp) -> str | None:
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not math.isfinite(timestamp):
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000, SHANGHAI).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


class HiThinkProvider:
    """Thread-safe client; all instances sharing a credential share a limiter.

    ``min_interval`` limits request starts, including retries. An instance does
    not serialize whole requests, so a slow HTTP response cannot hold a lock.
    ``max_retries`` can be reduced for a strict live polling deadline.
    """

    _limit_lock = threading.Lock()
    _next_request: dict[str, float] = {}
    _cooldown_until: dict[str, float] = {}

    def __init__(self, api_key: str, min_interval: float = 0.5, timeout: float = 6,
                 *, max_retries: int = 3):
        if not isinstance(api_key, str) or not api_key.strip():
            raise APIError("请先配置金融数据 API Key")
        self._api_key = api_key.strip()
        self._limiter_id = hashlib.sha256(self._api_key.encode()).hexdigest()
        self.min_interval = max(0.0, float(min_interval))
        self.timeout = max(0.1, float(timeout))
        self.max_retries = min(3, max(0, int(max_retries)))

    def __repr__(self):
        return "HiThinkProvider(api_key=<redacted>)"

    def _throttle(self):
        with self._limit_lock:
            now = time.monotonic()
            self._check_cooldown(now)
            slot = max(now, self._next_request.get(self._limiter_id, 0))
            self._next_request[self._limiter_id] = slot + self.min_interval
        if slot > now:
            time.sleep(slot - now)
        # Another client may have been limited while this request waited for a slot.
        with self._limit_lock:
            self._check_cooldown(time.monotonic())

    def _check_cooldown(self, now):
        """Caller holds _limit_lock. Refuse queued work instead of sleeping for it."""
        remaining = self._cooldown_until.get(self._limiter_id, 0) - now
        if remaining > 0:
            raise APIError("金融数据接口正在限流冷却，请稍后重试", code=4001,
                           retry_after_seconds=remaining)

    def _note_rate_limit(self, seconds):
        with self._limit_lock:
            self._cooldown_until[self._limiter_id] = max(
                self._cooldown_until.get(self._limiter_id, 0), time.monotonic() + seconds)

    def rate_limit_status(self):
        """Credential-free local diagnostics; no file or network access."""
        with self._limit_lock:
            remaining = max(0.0, self._cooldown_until.get(self._limiter_id, 0) - time.monotonic())
        return {"cooldown_seconds": remaining, "rate_limited": remaining > 0}

    @staticmethod
    def _retry_delay(attempt: int, retry_after=None):
        """Parse Retry-After without copying the header into errors or logs."""
        wait = 0.5 * (2 ** attempt)
        if retry_after:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds) and seconds >= 0:
                    wait = max(wait, seconds)
            except (ValueError, TypeError, OverflowError):
                try:
                    retry_time = parsedate_to_datetime(retry_after)
                    if retry_time.tzinfo is None:
                        retry_time = retry_time.replace(tzinfo=timezone.utc)
                    wait = max(wait, (retry_time - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return max(0.0, wait)

    def _backoff(self, attempt: int):
        time.sleep(self._retry_delay(attempt))

    def get(self, path, params=None, *, max_retries=None) -> dict:
        if not isinstance(path, str) or not path.startswith("/api/") or "?" in path or "#" in path:
            raise APIError("接口路径无效")
        if params and any("key" in str(key).lower() for key in params):
            raise APIError("认证信息仅允许通过请求头发送")
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = BASE_URL + path + (("?" + query) if query else "")
        retries = self.max_retries if max_retries is None else min(3, max(0, int(max_retries)))
        for attempt in range(retries + 1):
            self._throttle()
            request = Request(url, headers={"X-api-key": self._api_key,
                                           "Accept": "application/json",
                                           "User-Agent": "AuctionLab/1.4"})
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    headers = getattr(response, "headers", None)
                    retry_after = headers.get("Retry-After") if headers else None
                    payload = response.read(25_000_001)
                    if len(payload) > 25_000_000:
                        raise APIError("接口响应超过安全大小，请缩小请求")
                    if response.status != 200:
                        raise APIError("接口 HTTP 状态异常", code=response.status)
                envelope = json.loads(payload)
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After") if error.headers else None
                error.close()
                retryable = status == 429 or status in (500, 502, 503, 504)
                delay = self._retry_delay(attempt, retry_after) if retryable else None
                if status == 429:
                    self._note_rate_limit(delay)
                # Do not shorten an upstream delay to our blocking-sleep limit.
                # The scheduler can resume later, including single-attempt auctions.
                if retryable and attempt < retries and delay <= 15:
                    time.sleep(delay)
                    continue
                raise APIError("接口 HTTP 请求失败", code=status,
                               retry_after_seconds=delay) from None
            except (URLError, TimeoutError, OSError):
                if attempt < retries:
                    self._backoff(attempt)
                    continue
                raise APIError("网络请求超时或连接失败") from None
            except (ValueError, UnicodeError):
                raise APIError("接口返回了无效 JSON") from None
            if not isinstance(envelope, dict) or not isinstance(envelope.get("code"), int) or isinstance(envelope.get("code"), bool):
                raise APIError("接口响应信封缺少有效 code")
            code = envelope["code"]
            if code != 0:
                retryable = code == 4001 or code in (5001, 5002, 5003)
                delay = self._retry_delay(attempt, retry_after) if retryable else None
                if code == 4001:
                    self._note_rate_limit(delay)
                if retryable and attempt < retries and delay <= 15:
                    time.sleep(delay)
                    continue
                messages = {1001: "接口缺少参数", 1002: "接口参数格式错误", 1003: "接口参数超出范围",
                            1004: "接口参数冲突", 2001: "金融数据认证失败", 2003: "金融数据 Key 无效或无权限",
                            3001: "未找到标的", 3002: "上游数据尚未就绪", 3004: "标的不支持此接口",
                            4001: "金融数据接口限流"}
                request_id = envelope.get("request_id")
                if isinstance(request_id, str) and self._api_key in request_id:
                    request_id = None
                raise APIError(messages.get(code, "金融数据上游异常"), code=code,
                               request_id=request_id, retry_after_seconds=delay)
            data = envelope.get("data")
            if not isinstance(data, dict):
                raise APIError("成功信封中 data 无效")
            return data
        raise APIError("重试次数已用尽")

    @staticmethod
    def _items(data):
        rows = data.get("item")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise APIError("接口 item 字段格式错误")
        return rows

    @staticmethod
    def _codes(codes, limit=None):
        values = codes.split(",") if isinstance(codes, str) else list(codes)
        if not values or (limit is not None and len(values) > limit):
            raise APIError(f"每次请求标的数须为 1 至 {limit}" if limit else "标的列表不能为空")
        normalized = [str(code).strip().upper() for code in values]
        if any(not re.fullmatch(r"[A-Z0-9]+\.[A-Z]{2,4}", code) for code in normalized):
            raise APIError("请使用代码表提供的完整 thscode")
        return list(dict.fromkeys(normalized))

    def calendar(self) -> list[str]:
        data = self.get("/api/a-share/calendar/trading-days")
        days = []
        for row in self._items(data):
            value = row.get("date")
            try:
                days.append(datetime.strptime(value, "%Y%m%d").date().isoformat())
            except (ValueError, TypeError):
                raise APIError("交易日历日期格式错误") from None
        return sorted(set(days))

    def pool(self, kind, date) -> list[dict]:
        if kind not in ("limit-up", "limit-down", "limit-break"):
            raise APIError("股票池类型无效")
        path = f"/api/a-share/special-data/{kind}-pool"
        rows, seen, declared = [], set(), None
        for page in range(1, 501):
            data = self.get(path, {"date_ms": date_ms(date), "page": page, "size": 200})
            batch = self._items(data)
            paging = data.get("pagination")
            if not isinstance(paging, dict):
                raise APIError("股票池分页信息缺失")
            total, pages = paging.get("total"), paging.get("pages")
            echoed_page, size = paging.get("page"), paging.get("size")
            if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                   for value in (total, pages, echoed_page, size)):
                raise APIError("股票池分页信息无效")
            if echoed_page != page or size != 200:
                raise APIError("股票池分页回显与请求不一致，请重新获取")
            # Empty pools may conventionally declare zero or one total page.
            expected_pages = (total + size - 1) // size
            if (total and pages != expected_pages) or (not total and pages not in (0, 1)):
                raise APIError("股票池总页数与总条数不一致，请重新获取")
            if declared is None:
                declared = (total, pages)
            elif declared != (total, pages):
                raise APIError("股票池分页总数在读取期间变化，请重新获取")
            expected_size = max(0, min(size, total - (page - 1) * size))
            if len(batch) != expected_size:
                raise APIError("股票池分页数量不完整，请重新获取")
            for row in batch:
                code = row.get("thscode")
                if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code)
                        or code in seen):
                    raise APIError("股票池分页发生重复或缺码，请重新获取")
                seen.add(code)
                rows.append(row)
            if page >= pages:
                if len(rows) != total:
                    raise APIError("股票池分页数量不完整，请重新获取")
                return rows
            if not batch:
                raise APIError("股票池中间分页为空，数据不完整")
        raise APIError("股票池超过分页保护上限")

    def auction(self, codes, stage="live") -> dict:
        if stage not in ("live", "final"):
            raise APIError("竞价阶段必须为 live 或 final")
        codes = self._codes(codes, 100)
        # A missed live batch must not stall every other symbol behind retries.
        # The scheduler retries at its next cycle or bounded final grace window.
        return self.get("/api/a-share/auction/snapshot", {"thscodes": ",".join(codes), "stage": stage},
                        max_retries=0)

    def resolve_stock(self, code) -> dict:
        """Resolve only an exact A-share code through the official code table.

        Six-digit inputs are intentionally not assigned an exchange by prefix.
        The search endpoint is fuzzy; exact matching must happen in the client.
        """
        from .stocks import validate_code
        try:
            query = validate_code(code)
        except ValueError as exc:
            raise APIError(str(exc)) from None
        data = self.get("/api/meta/tickers/search", {"q": query, "asset_type": "a-share", "limit": 50})
        matches = {}
        for item in self._items(data):
            full = item.get("thscode")
            ticker = item.get("ticker")
            if (item.get("asset_type") != "a-share" or not isinstance(full, str)
                    or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", full)
                    or ticker != full[:6] or item.get("exchange") not in (None, full[-2:])):
                continue
            if ("." in query and full != query) or ("." not in query and ticker != query):
                continue
            if full in matches and matches[full] != item:
                raise APIError("官方代码表对同一证券返回冲突信息，请稍后重试")
            matches[full] = item
        if not matches:
            raise APIError("未在官方 A 股代码表找到精确匹配，请核对证券代码")
        if len(matches) != 1:
            raise APIError("六位代码对应多个 A 股标的，请输入带 .SH、.SZ 或 .BJ 的完整代码")
        metadata = copy.deepcopy(next(iter(matches.values())))
        if not isinstance(metadata.get("name"), str) or not metadata["name"].strip():
            raise APIError("官方代码表缺少证券名称，暂不能确认标的")
        metadata["resolution"] = {"input": query, "source": "official_ticker_search", "timestamp": data.get("timestamp")}
        return metadata

    def stock_quote(self, codes) -> dict:
        """One explicit-stock snapshot, keeping the official timestamp untouched."""
        normalized = self._codes(codes, 100)
        if any(not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code) for code in normalized):
            raise APIError("个股快照仅接受官方确认的完整 A 股代码")
        return self.get("/api/a-share/prices/snapshot", {"thscodes": ",".join(normalized)})

    def market(self) -> dict:
        rows, timestamps, declared_total, seen = [], [], None, set()
        # Snapshot's total denotes code-table size, not the number of ready quotes.
        for offset in range(0, 100_000, 100):
            data = self.get("/api/a-share/prices/snapshot", {"limit": 100, "offset": offset})
            total = data.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise APIError("全市场行情缺少有效分页总数")
            if declared_total is None:
                declared_total = total
            elif declared_total != total:
                raise APIError("全市场代码表总数在分页期间变化，请重新获取")
            batch = self._items(data)
            if len(batch) > max(0, min(100, total - offset)):
                raise APIError("全市场行情分页返回数量超出代码表范围")
            # A page may omit quotes that are not ready, including an empty page.
            for row in batch:
                code = row.get("thscode")
                if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code)
                        or code in seen):
                    raise APIError("全市场行情分页重复或缺少标的代码")
                seen.add(code)
                rows.append(row)
            timestamps.append(data.get("timestamp"))
            if offset + 100 >= declared_total:
                valid = [t for t in timestamps if iso_date(t)]
                return {"timestamp": max(valid) if valid else None, "page_timestamps": timestamps,
                        "total": declared_total, "item": rows}
        raise APIError("全市场行情超过分页保护上限")

    def catalog(self, tag="cn_concept") -> list[dict]:
        if tag not in ("cn_concept", "industry", "region", "tszs"):
            raise APIError("板块目录类型无效")
        return self._items(self.get("/api/a-share-index/catalog/ths-index-list", {"tag": tag}))

    def indices(self, codes) -> dict:
        return self.get("/api/a-share-index/prices/snapshot", {"thscodes": ",".join(self._codes(codes))})

    def members(self, code) -> list[dict]:
        return self._items(self.get("/api/a-share-index/constituents/ths-stock-list",
                                   {"thscode": self._codes([code], 1)[0]}))

    def ladder(self) -> dict:
        return self.get("/api/a-share/special-data/limit-up-ladder")

    def tickers(self, asset_type="a-share") -> list[dict]:
        rows, seen = [], set()
        for offset in range(0, 200_000, 1000):
            batch = self._items(self.get("/api/meta/tickers/list",
                                        {"asset_type": asset_type, "limit": 1000, "offset": offset}))
            if len(batch) > 1000:
                raise APIError("标的目录分页返回数量超出请求上限")
            for row in batch:
                code = row.get("thscode")
                if (not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]+\.[A-Za-z]{2,4}", code)
                        or code.upper() in seen):
                    raise APIError("标的目录分页重复或缺少有效代码")
                seen.add(code.upper())
            rows.extend(batch)
            if len(batch) < 1000:
                return rows
        raise APIError("标的目录超过分页保护上限")

    def historical(self, code, start, end, *, index=False, adjust="none") -> dict:
        start_ms = date_ms(start) if isinstance(start, str) else int(start)
        end_ms = date_ms(end) + 86_400_000 - 1 if isinstance(end, str) else int(end)
        if start_ms > end_ms:
            raise APIError("历史行情起始时间晚于结束时间")
        params = {"thscode": self._codes([code], 1)[0], "interval": "1d",
                  "start": start_ms, "end": end_ms}
        if not index:
            if adjust not in ("none", "forward", "backward"):
                raise APIError("历史行情复权方式无效")
            params["adjust"] = adjust
        domain = "a-share-index" if index else "a-share"
        return self.get(f"/api/{domain}/prices/historical", params)

    def index_historical(self, code, start, end) -> dict:
        return self.historical(code, start, end, index=True)
