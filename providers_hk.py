"""Data providers for the Hong Kong (HKEX) screener.

  roster       HKEX's own List of Securities (one xlsx, every line on the
               exchange): stock code, short name, category, sub-category
               (Main Board, GEM, investment company, depositary receipt,
               trading-only security), ISIN, and trading currency. The
               counterpart of Deutsche Börse's workbook and Euronext's
               download. It carries no industry - see config_hk.

  market caps  HKEX's equities feed, the data behind its own stock screener:
               one request returned a market cap for all ~2,800 lines until
               about 2026-10-15; since then it returns 26 rows (RMB counters
               and warrants) for every query. Kept as a market-cap pre-gate
               and fallback in case it comes back; a thin answer is logged.
               FOR AN H-SHARE IT IS THE H TRANCHE ONLY - PetroChina HK$217bn
               against Yahoo's HK$1.88tn for the whole company - so H-shares
               bypass that pre-gate and never take it as a fallback.

  liquidity    batched yf.download over EVERY roster line, before .info:
               median daily traded value over the lookback. It is the
               pre-gate that does not depend on HKEX (a line under the
               liquidity floor cannot pass the later gate, which reads the
               same numbers), and the liquidity gate itself. HKEX is the
               only venue for these lines, so unlike Xetra or Euronext this
               is the whole market's volume, not a lower bound.

  fundamentals yfinance .info on the Hong Kong line (0700.HK), one call per
               pre-gate survivor, fast_info as the market-cap fallback -
               France's snapshot(), fields unchanged, under RateGuard.

  statements   Yahoo annual income statement + balance sheet + seven years of
               daily closes. build_statement_record is the UK build's,
               unchanged since Poland.

THE CURRENCY TRAP, LARGE: most of Hong Kong by value reports in renminbi
(Tencent, ICBC, PetroChina, China Mobile) or US dollars (HSBC, AIA) and
quotes in Hong Kong dollars. Yahoo's market cap, EV, EPS and book value per
share on those lines are in Hong Kong dollars; its EBITDA and net income are
in the reporting currency. So P/E and P/B come out right and EV/EBITDA does
not - AIA's reads 86.5, a Hong Kong dollar EV over a US dollar EBITDA.
hk_filters.repair_foreign_reporters is France's repair, tested per row.

H-SHARE MARKET CAPS: Yahoo's marketCap on an H-share is the H price times
ALL the company's shares, A and H (ICBC: 7.545 x 356.4bn). That is the right
number for a size gate - it is the company's size - and it matches the filed
share count, so the history screen's basis check holds. Germany's dual-class
note, the same shape.
"""
from __future__ import annotations

import concurrent.futures as cf
import io
import json
import logging
import os
import re
import threading
import time

import numpy as np
import pandas as pd
import requests

import config_hk as K
from config_hk import ScreenConfig

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en"}

LIST_URL = ("https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/"
            "ListOfSecurities.xlsx")
EQUITIES_PAGE = "https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities?sc_lang=en"
FILTER_URL = ("https://www1.hkex.com.hk/hkexwidget/data/getequityfilter?lang=eng"
              "&token={token}&sort=5&order=0&all=1&qid={qid}&callback=cb")
YAHOO_SUFFIX = ".HK"
# A healthy feed carries ~2,800 HKD caps; from 2026-10-15 it carried none.
MIN_FEED_CAPS = 2000
# HKD is pegged to USD inside 7.75-7.85. Outside a slightly wider band a live
# rate is wrong, not news.
USD_PER_HKD_BAND = (1 / 7.95, 1 / 7.65)

# Every field snapshot() promises to return. Declared rather than inferred so a
# fully throttled fetch still produces a correctly-shaped frame.
SNAPSHOT_FIELDS = (
    "yf_name", "currency", "market_cap_local", "mcap_source", "close_local",
    "trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps",
    "book_value_ps", "div_yield", "yf_sector", "yf_industry", "yf_country",
    "quote_type", "yf_shares",
    # The currency the ACCOUNTS are in - Tencent quotes in HKD and reports in
    # renminbi, HSBC in US dollars.
    "fin_ccy",
    # The currency repair's evidence - see hk_filters.repair_foreign_reporters.
    "yf_ev", "yf_ebitda", "yf_ni", "yf_roe",
    # Share of the company held by insiders (family, parent) - the control
    # flag, config_hk.CONTROL_MIN_INSIDERS.
    "insiders_pct",
)


def is_rate_limit(e: Exception) -> bool:
    return "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e)


class RateGuard:
    """Pauses, then stops, a worker pool after a run of failed requests.

    The US build's. Yahoo's block is per IP and lasts minutes, so every
    request after it fails - and each one deepens it. After `limit`
    failures with no success in between, every worker pauses for `cooldown`
    seconds and the pool resumes - up to `max_cooldowns` times, then it
    stops asking; the cache keeps what arrived, the run refuses to screen a
    partial universe, and a re-run fills the gap.

    A failure is a rate-limit error, a .info with no price and no market
    cap, or an empty income statement. Hong Kong learned the middle one the
    hard way: from 2026-10-15, with HKEX's feed dead and the pre-gate off,
    Yahoo answered a third to a half of ~2,700 .info calls with empty dicts
    and no error, and the run published the 52-64% it got.
    """

    def __init__(self, what: str, limit: int = 25, cooldown: float = 120.0,
                 max_cooldowns: int = 3):
        self.what, self.limit = what, limit
        self.cooldown, self.max_cooldowns = cooldown, max_cooldowns
        self.n, self.used, self.tripped = 0, 0, False
        self.resume_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_cfg(cls, what: str, cfg: ScreenConfig) -> "RateGuard":
        return cls(what, cfg.guard_limit, cfg.guard_cooldown_s, cfg.guard_max_cooldowns)

    def gate(self) -> bool:
        """Called before each request: waits out a cooldown, False once tripped."""
        while True:
            with self._lock:
                if self.tripped:
                    return False
                wait = self.resume_at - time.time()
            if wait <= 0:
                return True
            time.sleep(min(wait, 5.0))

    def ok(self) -> None:
        with self._lock:
            self.n = 0

    def hit(self) -> None:
        with self._lock:
            self.n += 1
            if self.n < self.limit or self.tripped or time.time() < self.resume_at:
                return
            if self.used < self.max_cooldowns:
                self.used += 1
                self.n = 0
                self.resume_at = time.time() + self.cooldown
                log.warning("Yahoo: %d %s requests failed in a row - pausing %.0fs "
                            "(cooldown %d of %d)", self.limit, self.what, self.cooldown,
                            self.used, self.max_cooldowns)
            else:
                self.tripped = True
                log.error("Yahoo is rate-limiting this IP: %s requests still failing "
                          "after %d cooldowns. Stopping - the cache keeps what arrived; "
                          "re-run in a few minutes to fill the rest.", self.what, self.used)


def out_schema(df: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        df = pd.DataFrame({"ticker": tickers})
    df = df.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
    return df[df["ticker"].isin(tickers)].reset_index(drop=True)


def to_yahoo(code) -> str:
    """HKEX stock code -> Yahoo symbol: 00700 -> 0700.HK, 09988 -> 9988.HK.
    Yahoo writes at least four digits, so five-digit codes keep all five."""
    s = re.sub(r"\D", "", str(code or ""))
    return f"{int(s):04d}{YAHOO_SUFFIX}" if s else ""


def parse_securities_list(raw: pd.DataFrame) -> tuple[pd.DataFrame | None, str]:
    """The table on HKEX's List of Securities, and its "as at" date.

    Pure, so test_hongkong.py can plant the layout. The header row is found
    by its "Stock Code" cell rather than assumed (Germany's lesson: exchanges
    move things), header text is cut at its first line break ("Spread Table
    \\n1 = Part A..."), and only the first column of each name is kept, blank
    headers dropped - Germany's October-2026 crash was a repeated header.
    """
    first = raw.iloc[:, 0].astype(str).str.strip()
    hdr = raw.index[first.eq("Stock Code")]
    if hdr.empty:
        return None, ""
    asof = ""
    for v in raw.iloc[:hdr[0], 0].dropna():
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(v))
        if m:
            asof = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            break
    t = raw.iloc[hdr[0] + 1:].copy()
    names = pd.Index([str(c).split("\n")[0].strip() for c in raw.iloc[hdr[0]]])
    t.columns = names
    keep = ~names.duplicated() & ~names.isin(["", "nan", "None"])
    t = t.loc[:, keep]
    t = t[t["Stock Code"].astype(str).str.strip().str.fullmatch(r"\d{1,5}")]
    return t, asof


def parse_equity_filter(text: str) -> pd.DataFrame:
    """HKEX's equities feed (JSONP) -> code, market cap in HKD. Pure.

    Market caps arrive as "3,813.44" with a unit field (B / M / K); a blank
    cap is missing, never zero. The feed's own currency field is the trading
    currency of the line; only HKD lines are read.
    """
    cols = ["code", "hkex_mcap_hkd"]
    try:
        body = text[text.index("(") + 1:text.rindex(")")]
        rows = json.loads(body)["data"]["stocklist"]
    except Exception:                                     # noqa: BLE001
        return pd.DataFrame(columns=cols)
    unit = {"B": 1e9, "M": 1e6, "K": 1e3, "": 1.0}
    out = []
    for r in rows:
        if str(r.get("ccy") or "") != K.HOME_CURRENCY:
            continue
        v = str(r.get("mktcap") or "").replace(",", "").strip()
        try:
            cap = float(v) * unit.get(str(r.get("mktcap_u") or ""), np.nan)
        except ValueError:
            cap = np.nan
        out.append({"code": K.code5(r.get("sym")), "hkex_mcap_hkd": cap})
    return pd.DataFrame(out, columns=cols)


def token_from_page(html: str) -> str:
    """The feed's token, which HKEX's equities page embeds in its script
    (LabCI.getToken). The commented sample line above it is skipped."""
    m = re.search(r"getToken\s*=\s*function\s*\(\)\s*\{(.*?)\}", html, re.S)
    block = m.group(1) if m else html
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("//"):
            continue
        t = re.search(r'return\s+"([^"]{20,})"', line)
        if t:
            return t.group(1)
    return ""


class HKEXProvider:
    def __init__(self, cfg: ScreenConfig):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.roster_asof = ""

    # -- calendar ----------------------------------------------------------
    def recent_business_days(self, n: int) -> list[str]:
        """Real HKEX sessions, read off a liquid line - Hong Kong's holidays
        (Lunar New Year, Ching Ming, Chung Yeung) match no generic calendar,
        and a typhoon signal 8 closes the market at a few hours' notice."""
        import yfinance as yf
        try:
            h = yf.Ticker("0700.HK").history(period=f"{max(n * 2, 120)}d")
            if not h.empty:
                return [d.strftime("%Y-%m-%d") for d in h.index[-n:]]
        except Exception as e:                            # noqa: BLE001
            log.warning("calendar via yfinance failed: %s", e)
        days = pd.bdate_range(end=pd.Timestamp.today(), periods=n)
        return [d.strftime("%Y-%m-%d") for d in days]

    # -- roster ------------------------------------------------------------
    def listing_roster(self) -> pd.DataFrame:
        """Every equity and REIT line on HKEX.

        REITs are read so the funnel can count them before they are excluded;
        warrants, CBBCs, debt and ETPs are never read.
        """
        cols = ["ticker", "code", "symbol", "isin", "name", "category", "subcategory",
                "board", "trading_ccy"]
        try:
            r = self.s.get(LIST_URL, timeout=90)
            r.raise_for_status()
            raw = pd.read_excel(io.BytesIO(r.content), header=None, dtype=str)
        except Exception as e:                            # noqa: BLE001
            log.error("HKEX List of Securities failed: %s", e)
            return pd.DataFrame(columns=cols)
        t, asof = parse_securities_list(raw)
        if t is None:
            log.error("no 'Stock Code' header in the List of Securities - layout changed?")
            return pd.DataFrame(columns=cols)
        self.roster_asof = asof

        def col(name):
            return t[name] if name in t.columns else pd.Series("", index=t.index)

        df = pd.DataFrame({
            "code": col("Stock Code").map(K.code5),
            "name": col("Name of Securities").fillna("").astype(str).str.strip(),
            "category": col("Category").fillna("").astype(str).str.strip(),
            "subcategory": col("Sub-Category").fillna("").astype(str).str.strip(),
            "isin": col("ISIN").fillna("").astype(str).str.strip(),
            "trading_ccy": col("Trading Currency").fillna("").astype(str).str.strip(),
        })
        df = df[df["category"].isin([K.EQUITY_CATEGORY, K.REIT_CATEGORY])]
        df["board"] = df["subcategory"].map(K.board_of)
        df["symbol"] = df["code"]
        df["ticker"] = df["code"].map(to_yahoo)
        df = df[df["ticker"].ne("")].drop_duplicates(subset=["code"])
        log.info("  List of Securities as at %s: %d equity and REIT lines",
                 asof or "?", len(df))
        return df[cols].reset_index(drop=True)

    def market_caps(self) -> pd.DataFrame:
        """HKEX's own market cap for every HKD line, one request. Empty (and
        the run continues without the pre-gate) if the page or feed moves."""
        try:
            html = self.s.get(EQUITIES_PAGE, timeout=40).text
            token = token_from_page(html)
            if not token:
                raise RuntimeError("token not found on the equities page")
            r = self.s.get(FILTER_URL.format(token=token, qid=int(time.time() * 1000)),
                           headers={"Referer": "https://www.hkex.com.hk/"}, timeout=60)
            r.raise_for_status()
            out = parse_equity_filter(r.text)
            n = int(out["hkex_mcap_hkd"].notna().sum())
            log.info("  HKEX market caps for %d lines", n)
            if n < MIN_FEED_CAPS:
                log.warning("HKEX's equities feed answered only %d HKD market caps (it "
                            "carried ~2,800 until 2026-10-15) - no market-cap pre-gate or "
                            "fallback; the turnover pre-gate still applies", n)
            return out
        except Exception as e:                            # noqa: BLE001
            log.warning("HKEX equities feed failed (%s) - no pre-gate, every line "
                        "goes to Yahoo", e)
            return pd.DataFrame(columns=["code", "hkex_mcap_hkd"])

    # -- fundamentals ------------------------------------------------------
    def _cache_path(self, asof: str) -> str:
        return os.path.join(self.cfg.cache_dir, f"snapshot_{asof or 'latest'}.csv")

    def snapshot(self, tickers: list[str], asof: str = "") -> pd.DataFrame:
        """One yfinance .info per ticker, fast_info when .info has no market cap.

        Cached per session date for the reason the UK build found: Yahoo
        throttles by returning dicts with fields MISSING, not by raising, so
        successive runs must fill gaps rather than re-ask for everything.
        """
        import yfinance as yf

        cached = pd.DataFrame()
        path = self._cache_path(asof)
        if os.path.exists(path):
            age_h = (time.time() - os.path.getmtime(path)) / 3600.0
            if age_h <= self.cfg.cache_ttl_hours:
                try:
                    cached = pd.read_csv(path)
                    log.info("  cache: %d rows from %s (%.1fh old)",
                             len(cached), os.path.basename(path), age_h)
                except Exception as e:                    # noqa: BLE001
                    log.warning("  cache unreadable (%s), refetching", e)
                stale = [f for f in SNAPSHOT_FIELDS if f not in cached.columns]
                if stale and not cached.empty:
                    log.info("  cache predates field(s) %s - refetching",
                             ", ".join(stale))
                    cached = pd.DataFrame()

        have = set()
        if not cached.empty and "market_cap_local" in cached.columns:
            have = set(cached.loc[cached["market_cap_local"].notna(), "ticker"])
        todo = [t for t in tickers if t not in have]
        if not todo:
            log.info("  all %d tickers served from cache", len(tickers))
            return out_schema(cached, tickers)

        # The limit is global (it rejects the crumb request), so probe once
        # single-threaded before opening the pool.
        try:
            yf.Ticker(todo[0]).info
        except Exception as e:                            # noqa: BLE001
            if "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e):
                log.error("Yahoo is rate-limiting this IP (%s). Nothing was "
                          "fetched; wait a few minutes and re-run - the cache "
                          "keeps whatever has already arrived.", type(e).__name__)
                return out_schema(cached, tickers)
            log.debug("probe ticker %s failed non-fatally: %s", todo[0], e)

        guard = RateGuard.from_cfg("snapshot", self.cfg)

        def one(t: str) -> dict:
            rec = {"ticker": t}
            i = {}
            if self.cfg.request_delay:
                time.sleep(self.cfg.request_delay)
            for attempt in (0, 1):
                if not guard.gate():
                    return rec
                try:
                    i = yf.Ticker(t).info or {}
                    if i.get("marketCap") is not None or i.get("regularMarketPrice"):
                        guard.ok()
                        break
                    # No price and no cap, no error: Yahoo shedding load (or
                    # a dead line). Counted, so a run of them is a cooldown,
                    # not a quietly thinner market; the retry waits it out.
                    guard.hit()
                except Exception as e:                    # noqa: BLE001
                    log.debug("%s attempt %d: %s", t, attempt, e)
                    if is_rate_limit(e):
                        guard.hit()
                        continue
                if attempt == 0:
                    time.sleep(0.5 + self.cfg.request_delay)
            if not (i.get("marketCap") is not None or i.get("regularMarketPrice")
                    or i.get("currentPrice")):
                return rec
            price = i.get("regularMarketPrice") or i.get("currentPrice")
            mcap, src = i.get("marketCap"), "info"
            if mcap is None and price:
                try:
                    mcap = yf.Ticker(t).fast_info.get("marketCap")
                    src = "fast_info" if mcap else ""
                except Exception as e:                    # noqa: BLE001
                    log.debug("%s fast_info: %s", t, e)
                    src = ""
            rec.update({
                "yf_name": i.get("longName") or i.get("shortName"),
                "currency": i.get("currency"),
                "market_cap_local": mcap,
                "mcap_source": src if mcap else "",
                "close_local": price,
                "trailing_pe": i.get("trailingPE"),
                "price_to_book": i.get("priceToBook"),
                "ev_to_ebitda": i.get("enterpriseToEbitda"),
                "trailing_eps": i.get("trailingEps"),
                "book_value_ps": i.get("bookValue"),
                # Percent (3.96 = 3.96%), the same scale as the 2% floor.
                "div_yield": i.get("dividendYield"),
                "yf_sector": i.get("sector"),
                "yf_industry": i.get("industry"),
                "yf_country": i.get("country"),
                "quote_type": i.get("quoteType"),
                # All shares, A and H: ICBC's sharesOutstanding is its 86.8bn
                # H-shares, impliedSharesOutstanding the 356.4bn its market
                # cap is built on.
                "yf_shares": i.get("impliedSharesOutstanding") or i.get("sharesOutstanding"),
                "fin_ccy": i.get("financialCurrency"),
                "yf_ev": i.get("enterpriseValue"),
                "yf_ebitda": i.get("ebitda"),
                "yf_ni": i.get("netIncomeToCommon"),
                "yf_roe": i.get("returnOnEquity"),
                "insiders_pct": i.get("heldPercentInsiders"),
            })
            return rec

        log.info("  fetching %d tickers (%d already cached)", len(todo), len(have))
        rows: list[dict] = []

        def run_pool(batch: list[str]) -> None:
            with cf.ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
                for n, rec in enumerate(ex.map(one, batch), 1):
                    rows.append(rec)
                    if n % 50 == 0:
                        log.info("  fetched %d/%d", n, len(batch))
                    # Save as we go: a run killed half-way keeps what it fetched.
                    if n % 400 == 0:
                        self._save_snapshot(cached, rows, asof)

        run_pool(todo)
        # Second pass: the ~dozen lines per shedding stretch that failed both
        # attempts before the guard paused. Not after a trip - Yahoo is
        # still refusing, and the run will refuse to screen anyway.
        priced = {r["ticker"] for r in rows if r.get("market_cap_local") is not None}
        again = [t for t in todo if t not in priced]
        if again and not guard.tripped and len(again) < len(todo):
            log.info("  second pass: %d unpriced lines, asking again in %.0fs",
                     len(again), self.cfg.snapshot_second_pass_pause_s)
            time.sleep(self.cfg.snapshot_second_pass_pause_s)
            first_used = guard.used
            guard = RateGuard.from_cfg("snapshot (second pass)", self.cfg)
            run_pool(again)
            guard.used += first_used
            log.info("  second pass priced %d of %d",
                     sum(1 for r in rows[-len(again):] if r.get("market_cap_local") is not None),
                     len(again))

        out = self._save_snapshot(cached, rows, asof)
        out = out[out["ticker"].isin(tickers)].reset_index(drop=True)
        got = int(out["market_cap_local"].notna().sum())
        fb = int((out["mcap_source"] == "fast_info").sum())
        log.info("  %d/%d tickers have a market cap (%d via fast_info)%s",
                 got, len(tickers), fb,
                 f"; {guard.used} cooldown(s)" if guard.used else "")
        return out

    def _save_snapshot(self, cached: pd.DataFrame, rows: list[dict], asof: str) -> pd.DataFrame:
        fresh = pd.DataFrame(rows)
        parts = [d for d in (cached, fresh) if not d.empty]
        out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        out = out.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
        # Priced rows first so a cached hit beats a fresh throttled blank.
        out = out.sort_values("market_cap_local", na_position="last")
        out = out.drop_duplicates(subset=["ticker"], keep="first")
        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            out.to_csv(self._cache_path(asof), index=False, encoding="utf-8")
        except Exception as e:                            # noqa: BLE001
            log.warning("  could not write cache: %s", e)
        return out

    def quarterly_equity(self, tickers: list[str]) -> dict[str, float]:
        """Latest interim common equity, in the reporting currency.

        France's reason: Yahoo's bookValue is the latest period's equity over
        today's shares, and the annual figure drifts from it. Hong Kong
        companies file half-yearly, so "quarterly" here is usually the
        interim; Yahoo serves it under the same call.
        """
        import yfinance as yf
        out: dict[str, float] = {}

        def one(t: str):
            if self.cfg.request_delay:
                time.sleep(self.cfg.request_delay)
            try:
                q = yf.Ticker(t).quarterly_balance_sheet
                for row in ("Common Stock Equity", "Stockholders Equity"):
                    if q is not None and row in q.index:
                        s = pd.to_numeric(q.loc[row], errors="coerce").dropna()
                        if not s.empty:
                            s.index = pd.to_datetime(s.index)
                            return t, float(s.sort_index().iloc[-1])
            except Exception as e:                        # noqa: BLE001
                log.debug("%s quarterly balance sheet: %s", t, e)
            return t, None

        with cf.ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            for t, v in ex.map(one, tickers):
                if v is not None:
                    out[t] = v
        log.info("  interim equity for %d of %d foreign reporters", len(out), len(tickers))
        return out

    # -- liquidity ---------------------------------------------------------
    def price_panel(self, tickers: list[str], days: int, chunk: int = 80) -> pd.DataFrame:
        """Daily close and volume for every ticker at once (batched endpoint,
        not .info's throttle)."""
        import yfinance as yf
        period = f"{max(int(days * 1.6), 30)}d"
        frames = []
        for i in range(0, len(tickers), chunk):
            part = tickers[i:i + chunk]
            try:
                px = yf.download(part, period=period, interval="1d", progress=False,
                                 auto_adjust=False, actions=False, threads=True,
                                 group_by="column")
            except Exception as e:                        # noqa: BLE001
                log.warning("price chunk %d failed: %s", i // chunk, e)
                continue
            if px is not None and not px.empty:
                frames.append(px)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    def liquidity(self, tickers: list[str], days: int, retry_pause: float = 20.0) -> pd.DataFrame:
        """average_daily_value over a fresh panel, with one second pass for
        the lines the first left out.

        yf.download fails per ticker without raising - a throttled chunk
        comes back as empty columns - so a missing line is asked again once,
        after a pause, before it is reported missing. What is still missing
        is left to the caller (the pre-gate sends it on to .info rather than
        guess it illiquid).
        """
        tickers = list(dict.fromkeys(tickers))
        adv = self.average_daily_value(self.price_panel(tickers, days), tickers, days)
        miss = [t for t in tickers if t not in set(adv["ticker"])]
        if miss:
            log.info("  turnover: %d of %d lines missing - asking again in %.0fs",
                     len(miss), len(tickers), retry_pause)
            time.sleep(retry_pause)
            more = self.average_daily_value(self.price_panel(miss, days), miss, days)
            adv = pd.concat([adv, more], ignore_index=True)
        n = adv["ticker"].nunique() if not adv.empty else 0
        log.info("  turnover for %d of %d lines", n, len(tickers))
        if n < 0.5 * len(tickers):
            log.warning("Yahoo's price download answered for only %d of %d lines - "
                        "throttled? The rest go on to .info unjudged.", n, len(tickers))
        return adv.drop_duplicates(subset=["ticker"]).reset_index(drop=True)

    @staticmethod
    def average_daily_value(panel: pd.DataFrame, tickers: list[str],
                            days: int) -> pd.DataFrame:
        """Median daily traded value in HKD over the last `days` sessions.

        Median, not mean: one index-rebalance day can be twenty times a name's
        normal volume. A suspended line trades nothing and its median is
        zero, so the gate removes it - Hong Kong has long suspensions that
        Yahoo keeps quoting at the last price.
        """
        if panel.empty:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        try:
            close, vol = panel["Close"], panel["Volume"]
        except KeyError:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        if isinstance(close, pd.Series):                  # single ticker
            close, vol = close.to_frame(tickers[0]), vol.to_frame(tickers[0])
        rows = []
        for t in tickers:
            if t not in close.columns or t not in vol.columns:
                continue
            c, v = close[t], vol[t]
            if isinstance(c, pd.DataFrame):               # duplicate column
                c, v = c.iloc[:, 0], v.iloc[:, 0]
            tv = (pd.to_numeric(c, errors="coerce")
                  * pd.to_numeric(v, errors="coerce")).dropna().tail(days)
            if tv.empty:
                continue
            rows.append({"ticker": t, "adv_local": float(tv.median()),
                         "adv_sessions": int(len(tv))})
        return pd.DataFrame(rows)

    # -- FX ----------------------------------------------------------------
    def _rate(self, base: str, quote: str, lo: float, hi: float) -> float:
        """Units of `quote` per one `base`: Yahoo, then Frankfurter (ECB)."""
        import yfinance as yf
        try:
            h = yf.Ticker(f"{base}{quote}=X").history(period="5d")
            if not h.empty:
                rate = float(h["Close"].iloc[-1])
                if lo < rate < hi:
                    return rate
        except Exception as e:                            # noqa: BLE001
            log.info("FX %s/%s via Yahoo failed (%s)", base, quote, e)
        try:
            r = self.s.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}",
                           timeout=20)
            rate = float(r.json()["rates"][quote])
            if lo < rate < hi:
                log.info("FX %s/%s from Frankfurter (ECB reference)", base, quote)
                return rate
        except Exception as e:                            # noqa: BLE001
            log.warning("FX %s/%s via Frankfurter failed: %s", base, quote, e)
        return float("nan")

    def hkd_to_usd(self) -> float:
        """USD per HKD - multiplied, the UK's and Germany's direction. The peg
        holds it inside 7.75-7.85 HKD per USD, so the fallback is close."""
        rate = self._rate("HKD", "USD", *USD_PER_HKD_BAND)
        if np.isfinite(rate):
            return rate
        log.warning("FX: both live sources failed, using the peg's midpoint "
                    "(7.80 HKD per USD).")
        return 1 / 7.80

    def hkd_per(self, currencies) -> dict[str, float]:
        """HKD per one unit of each reporting currency, for the currency
        repair. A rate that cannot be found is NaN, and the repair then
        refuses the affected multiples rather than guessing."""
        out = {"HKD": 1.0}
        for c in sorted({str(c) for c in currencies if isinstance(c, str) and c}):
            if c not in out:
                out[c] = self._rate(c, "HKD", 1e-4, 1e4)
                log.info("  FX: 1 %s = %.4f HKD", c, out[c])
        return out

    def hkd_per_ttm(self, currencies, spot: dict) -> dict[str, float]:
        """HKD per reporting unit, averaged over the last year of sessions.

        Yahoo converts a trailing EPS - four quarters' earnings - at about
        the year's average rate, not today's. The renminbi rose from 1.09 to
        1.17 HKD over the year to 2026-10-14, so testing EPS against spot put
        a quarter of renminbi reporters near the midpoint between the two
        hypotheses; against the average they sit at 0.98 (see
        hk_filters.repair_foreign_reporters). Falls back to spot.
        """
        import yfinance as yf
        out = {"HKD": 1.0}
        for c in sorted({str(c) for c in currencies if isinstance(c, str) and c}):
            if c in out:
                continue
            out[c] = spot.get(c, float("nan"))
            try:
                h = yf.Ticker(f"{c}HKD=X").history(period="1y")["Close"].dropna()
                if len(h) > 150:
                    out[c] = float(h.mean())
            except Exception as e:                        # noqa: BLE001
                log.info("FX %s/HKD history failed (%s) - spot used for the EPS test", c, e)
        return out


# ---------------------------------------------------------------------------
# Filed statements: three-year history and the own-history benchmark
# ---------------------------------------------------------------------------
# Ported from the UK build unchanged in logic, via Germany. The worked examples
# in the comments below (NatWest, Reckitt, Shell, Craneware) were measured in
# London; the rules they justify are Yahoo's, so they hold for .HK lines too.
# The Hong Kong cases that exercise them are in test_hongkong.py.
# Row names Yahoo uses in its annual statements, first match wins. Net income
# is taken to COMMON shareholders where Yahoo separates it: NatWest's differs
# by ~6% because of AT1 coupons, and a P/E is a price for the common equity.
IS_ROWS = {
    "rev": ("Total Revenue", "Operating Revenue"),
    "op": ("Operating Income", "Total Operating Income As Reported"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "np": ("Net Income Common Stockholders", "Net Income"),
    # Yahoo's "normalized" lines strip exactly its Total Unusual Items row. The
    # 3-year growth history stays on the REPORTED rows above, as Korea's does
    # - that is what happened. The own-history VALUATION uses these instead,
    # because a one-off is not a change in what the business is worth: Reckitt
    # sold Essential Home in 2025, reported EBITDA jumped 4,760 vs 3,966
    # normalized and net income 3,182 vs 2,535, and on reported figures it
    # passed the history screen on P/E and EV/EBITDA while its P/B - which a
    # disposal gain does not move - sat only 15% below its history. For an
    # ordinary year the two lines agree to within a few percent.
    "np_norm": ("Normalized Income",),
    "ebitda_norm": ("Normalized EBITDA",),
}
# Cash-flow rows, latest filed year only: free cash flow for P/FCF, and what
# the company paid its shareholders for the shareholder yield. Yahoo signs
# outflows negative (dividends, repurchases, capex); they are stored as
# positive amounts. "Free Cash Flow" is Yahoo's operating cash flow less
# capital expenditure; where it is absent the two parts are used.
CF_ROWS = {
    "fcf": ("Free Cash Flow",),
    "ocf": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "capex": ("Capital Expenditure",),
    "div_paid": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
    "buyback": ("Repurchase Of Capital Stock", "Common Stock Payments"),
    "issuance": ("Issuance Of Capital Stock", "Common Stock Issuance"),
}
BS_ROWS = {
    "equity": ("Common Stock Equity", "Stockholders Equity"),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "debt": ("Total Debt",),
    "cash": ("Cash And Cash Equivalents",
             "Cash Cash Equivalents And Short Term Investments"),
    "mi": ("Minority Interest",),
}

# Quote currencies Yahoo reports in minor units, and the major unit each is.
MINOR_CCY = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
             "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

# Consecutive filed share counts outside this band are treated as a corporate
# action (consolidation, split, rights issue) rather than buybacks. Shell's
# heavy buybacks move ~7% a year, so the band is wide enough to leave them
# alone. It cannot catch small consolidations; it catches the ones that would
# otherwise manufacture a 30%+ "discount" out of a unit change.
SHARE_BREAK_BAND = (0.67, 1.5)
# Today's price x latest filed shares must land near today's market cap,
# once any split or consolidation since the filing is allowed for. If it does
# not, the price series and the share count are on different bases and every
# historical market value built from them is wrong by the same factor.
#
# Measured 2026-10-06 on names with no split since their filing: UK 1st-99th
# percentile 0.75-1.07 (max 1.12), Germany 0.91-1.10. Below 1 is shares issued
# since the filing (IQE 0.73, Rockhopper 0.79), above it buybacks. The old
# 0.6-1.6 admitted an unrestated 3:2 split (0.67) or 4:3 consolidation (1.33)
# - a fake 25-33% move in every past valuation. 0.7-1.25 refuses both and
# keeps every healthy name measured.
NOW_BASIS_BAND = (0.7, 1.25)


def _splits_after(splits, when) -> list:
    """Split / consolidation ratios Yahoo recorded after `when`, oldest first.

    Yahoo reports a consolidation as a ratio below one (Johnson Matthey's
    4-for-3 in Aug 2026 is 0.75) and a demerger price adjustment the same way,
    so the caller decides what a ratio means by whether it explains the share
    count, never by its size alone.
    """
    if splits is None or len(splits) == 0:
        return []
    s = pd.to_numeric(splits, errors="coerce")
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize() \
        if getattr(s.index, "tz", None) is not None else pd.to_datetime(s.index).normalize()
    s = s[(s.index > when) & (s > 0) & (s != 1)].sort_index()
    return [float(v) for v in s.tolist()]
# A latest filing older than this is not the latest filing - Yahoo has missed
# one. "Today" is then today's price over earnings from two years ago, while
# yfinance's own twelve-month figure knows better: Craneware read 52.6x on
# the same basis against 28.6x trailing, and passed the history screen on that
# gap. 13 of 251 survivors (Sept 2026) had a newest filed year of 2024. Eighteen
# months leaves room for a late filer without admitting a skipped year.
MAX_FILING_AGE_DAYS = 548

# The statements cache stores DERIVED records, not raw frames (seven years of
# daily prices per name would be ~40x larger). So a change to
# build_statement_record does not reach names already cached - bump this and
# the next run refetches instead of serving numbers the new code would not
# produce.
STATEMENTS_CACHE_VERSION = 6        # 5: cash-flow fields; 6: four-quarter TTM


def cagr(values: list) -> float:
    """Compound annual growth across the span the values actually cover.

    Identical to the Korea build: undefined when the base is zero or negative.
    A company that lost money three years ago has no meaningful growth RATE,
    and inventing one is worse than reporting nothing. The yearly figures
    always ship alongside, so nothing is hidden by this.
    """
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return np.nan
    return (vals[-1] / vals[0]) ** (1.0 / (len(vals) - 1)) - 1.0


def major_ccy(ccy) -> tuple[str, float]:
    """(major currency, divisor) for a Yahoo quote currency."""
    c = str(ccy or "")
    return MINOR_CCY.get(c, (c, 1.0))


def _row(df: pd.DataFrame, names) -> pd.Series:
    """First matching statement row, indexed by fiscal year-end, oldest first."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            return s.sort_index()
    return pd.Series(dtype=float)


def _at(series: pd.Series, when: pd.Timestamp, tolerance_days: int = 10) -> float:
    """Last value on or before `when`, if it is within `tolerance_days`. A
    fiscal year-end that falls in a data gap is missing, not the nearest price
    from months earlier."""
    if series is None or series.empty:
        return np.nan
    s = series.loc[:when].dropna()
    if s.empty or (when - s.index[-1]).days > tolerance_days:
        return np.nan
    return float(s.iloc[-1])


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return np.nan
    return f if np.isfinite(f) else np.nan


def build_statement_record(inc: pd.DataFrame, bs: pd.DataFrame,
                           px: pd.Series, fx: pd.Series | None,
                           quote_ccy: str, fin_ccy: str,
                           close_now: float, mcap_now: float,
                           fin_years: int = 3, max_hist: int = 5,
                           asof: str | pd.Timestamp | None = None,
                           splits: pd.Series | None = None,
                           cf: pd.DataFrame | None = None,
                           qinc: pd.DataFrame | None = None) -> dict:
    """Everything the two statement-based features need, from raw frames.

    Pure: no network, so test_hongkong.py can exercise every trap offline. `px` is
    the daily close in QUOTE units (Hong Kong dollars for every .HK line); `fx` converts the
    quote's MAJOR currency into the reporting currency (None when they are the
    same); `close_now` and `mcap_now` are in the quote's major currency.

    Every multiple is a market value over a reported total, never a price over
    a per-share figure. Yahoo's per-share rows are in reporting-currency units
    against a price in the quote currency (Tencent: renminbi against Hong Kong dollars), and
    its EPS row is frequently rounded to zero; totals over totals sidestep both.

    Three-year history: the last `fin_years` filed years, oldest first, in
    millions of the REPORTING currency. Deliberately not converted - a growth
    rate should describe the business, not the renminbi against the Hong Kong dollar.

    Own history: for each filed year, that year-end market value over that
    year's filed figures, converted into the reporting currency at that
    year-end rate. Plus the latest filing's components, so the screen can put
    TODAY's market value over them on exactly the same definitions - see
    hk_filters.apply_history_screen for why that matters.
    """
    rec: dict = {"fin_ccy": fin_ccy or ""}

    rows = {k: _row(inc, v) for k, v in IS_ROWS.items()}
    rows.update({k: _row(bs, v) for k, v in BS_ROWS.items()})

    # A fiscal year counts as filed when it has revenue or net income. Yahoo
    # pads the frame with an extra, entirely empty oldest column (2021 in
    # every UK name checked, and in German, Polish, French and Hong Kong ones too), and that must not be read
    # as a zero year.
    dates = sorted(set(rows["rev"].dropna().index) | set(rows["np"].dropna().index))
    if not dates:
        rec["hist_note"] = "no filed years"
        return rec

    # ---- three-year history ---------------------------------------------
    fy = dates[-fin_years:]
    rec["fin_years"] = ",".join(d.strftime("%Y") for d in fy)
    rec["fin_n"] = len(fy)
    for key in ("rev", "op", "ebitda", "np"):
        series = [_num(rows[key].get(d)) / 1e6 for d in fy]
        for i, v in enumerate(series, 1):
            rec[f"{key}_y{i}"] = round(v, 1) if np.isfinite(v) else np.nan
        rec[f"{key}_cagr"] = cagr(series)

    # ---- own history ------------------------------------------------------
    hy = dates[-max_hist:]
    rec["hist_years"] = ",".join(d.strftime("%Y") for d in hy)
    q_major, q_div = major_ccy(quote_ccy)
    same_ccy = bool(fin_ccy) and fin_ccy == q_major
    # No reporting currency is not the same as Hong Kong dollars. Assuming it would
    # treat a dollar reporter's accounts as pounds and scale every historical
    # multiple by the exchange rate - a fake discount or premium of 20-35%.
    # Missing means no benchmark (invariant 2), never a guess.
    if not fin_ccy:
        rec["hist_note"] = "no reporting currency"

    def fx_at(when):
        if same_ccy:
            return 1.0
        return _at(fx, when) if fx is not None else np.nan

    shares = [_num(rows["shares"].get(d)) for d in hy]
    lf = hy[-1]

    # Earnings basis for the VALUATION: normalized where Yahoo has it for
    # every year in the window, reported otherwise - never a mix within one
    # company's series, because a history that switches definition part-way
    # compares a year with itself on two different measures (see IS_ROWS).
    def pick(norm_key, rep_key):
        n = rows[norm_key]
        if all(np.isfinite(_num(n.get(d))) for d in hy):
            return n, "normalized"
        return rows[rep_key], "reported"
    ni_row, ni_basis = pick("np_norm", "np")
    eb_row, _ = pick("ebitda_norm", "ebitda")
    rec["hist_earnings"] = ni_basis

    # Guard 0: a stale latest filing. Checked first, because every other
    # number in the record would be struck against it.
    if asof is not None and "hist_note" not in rec:
        age = (pd.Timestamp(asof).normalize() - lf).days
        if age > MAX_FILING_AGE_DAYS:
            rec["hist_note"] = "stale filings"

    # Guard 1: a share count that jumps between filings is a corporate action.
    # The price series is split-adjusted while filed counts are not
    # necessarily, so a market value built across the break is wrong by the
    # split ratio - which the screen would read as a huge discount.
    valid = [s for s in shares if np.isfinite(s) and s > 0]
    for a, b in zip(valid, valid[1:]):
        if not (SHARE_BREAK_BAND[0] <= b / a <= SHARE_BREAK_BAND[1]):
            rec["hist_note"] = "share-count break"
            break

    # Guard 2: today's price x latest filed shares against today's market cap,
    # allowing for a split or consolidation Yahoo has not restated yet.
    #
    # Yahoo's price series is adjusted for every split it records; its filed
    # share counts catch up later. Johnson Matthey consolidated 4-for-3 in Aug
    # 2026: the prices were rescaled at once, the FY2026 filing still carried
    # the old 167.9m shares against 125.9m today. The ratio here read 1.33,
    # inside the old 0.6-1.6 band, so every past market value was a third too
    # high and the history screen showed a ~20% discount to its own history
    # that did not exist. A 3:2 split would do the same in the other
    # direction. So, as the Japan build does: among "no change" and each
    # suffix of the ratios recorded since the filing, take the one that
    # explains today's count, restate every filed year by it, and refuse a gap
    # nothing explains.
    s_lf = _num(rows["shares"].get(lf))
    g = 1.0
    if "hist_note" not in rec and np.isfinite(s_lf) and s_lf > 0:
        mc = _num(mcap_now)
        ratio = (_num(close_now) * s_lf / mc) if mc else np.nan
        rec["basis_ratio"] = ratio
        if np.isfinite(ratio) and ratio > 0:
            fs = _splits_after(splits, lf)
            cands = [1.0] + [float(np.prod(fs[k:])) for k in range(len(fs))]
            g = min(cands, key=lambda c: abs(np.log(ratio * c)))
        if not (np.isfinite(ratio) and NOW_BASIS_BAND[0] <= ratio * g <= NOW_BASIS_BAND[1]):
            rec["hist_note"] = "price/share basis mismatch"
            g = 1.0
    # The factor the latest filing's per-share basis is off by: also what the
    # screen uses to check Yahoo's .info per-share fields (see the filters).
    rec["share_basis_g"] = g
    rec["lf_shares"] = s_lf
    shares = [s * g for s in shares]

    per, pbr, evx = [], [], []
    for d, sh in zip(hy, shares):
        p = _at(px, d) / q_div if px is not None else np.nan
        mv = p * sh * fx_at(d)        # year-end market value, reporting ccy
        ni, eq = _num(ni_row.get(d)), _num(rows["equity"].get(d))
        eb = _num(eb_row.get(d))
        debt, cash = _num(rows["debt"].get(d)), _num(rows["cash"].get(d))
        mi = _num(rows["mi"].get(d))
        mi = 0.0 if not np.isfinite(mi) else mi
        # Non-positive denominators become NaN here; bounds are applied by the
        # screen, which is also where a loss year drops out of the benchmark.
        per.append(mv / ni if np.isfinite(mv) and ni > 0 else np.nan)
        pbr.append(mv / eq if np.isfinite(mv) and eq > 0 else np.nan)
        ev = mv + debt - cash + mi
        evx.append(ev / eb if np.isfinite(ev) and eb > 0 else np.nan)

    if "hist_note" in rec:
        per = pbr = evx = []
    rec["hist_per"] = [round(v, 2) if np.isfinite(v) else None for v in per]
    rec["hist_pbr"] = [round(v, 3) if np.isfinite(v) else None for v in pbr]
    rec["hist_evx"] = [round(v, 2) if np.isfinite(v) else None for v in evx]

    # Latest filing's components, for today's multiples on the same basis.
    fx_last = np.nan
    if same_ccy:
        fx_last = 1.0
    elif fx is not None and not fx.dropna().empty:
        fx_last = float(fx.dropna().iloc[-1])
    rec.update({
        # On the same earnings basis as the history, or today's value would be
        # compared across two definitions - the thing this whole design avoids.
        "lf_ni": _num(ni_row.get(lf)), "lf_equity": _num(rows["equity"].get(lf)),
        "lf_ebitda": _num(eb_row.get(lf)), "lf_debt": _num(rows["debt"].get(lf)),
        "lf_cash": _num(rows["cash"].get(lf)), "lf_mi": _num(rows["mi"].get(lf)),
        # Today's quote-major -> reporting-currency rate, carried so the screen
        # does not have to look it up again.
        "fx_now": fx_last,
    })
    rec.update(cashflow_fields(cf, lf))
    # The latest filed year's reported operating and net profit - the one-off
    # guard's fallback where four quarters are not available.
    rec["lf_op"] = _num(rows["op"].get(lf))
    rec["lf_np_rep"] = _num(rows["np"].get(lf))
    rec.update(ttm_fields(qinc, asof))
    rec.setdefault("hist_note", "")
    return rec


# Trailing operating and net profit from the last four QUARTERS, both from the
# same statement. Not .info's operatingMargins: that is the latest quarter's
# margin, and times trailing revenue it put AECOM's and Primoris's operating
# profit below zero and Intuit's at 0.61 of the year (its seasonal quarter) -
# 141 one-off flags on the first run, most of them false.
TTM_ROWS = {"op": ("Operating Income", "Total Operating Income As Reported"),
            "ni": ("Net Income Common Stockholders", "Net Income")}
# Four quarters spanning no more than this many days from first to last
# period-end (a missing quarter would stretch it), the latest no older than
# MAX_TTM_AGE_DAYS. Half-yearly reporters (much of London, Paris, Hong Kong)
# rarely have four, and fall back to the filed year.
TTM_MAX_SPAN_DAYS = 300
MAX_TTM_AGE_DAYS = 200


def ttm_fields(qinc: pd.DataFrame | None, asof=None) -> dict:
    """Sum of the last four quarters' operating and net profit. Pure."""
    out = {"ttm_op": np.nan, "ttm_ni": np.nan}
    if qinc is None or qinc.empty:
        return out
    op, ni = _row(qinc, TTM_ROWS["op"]), _row(qinc, TTM_ROWS["ni"])
    both = pd.concat([op, ni], axis=1, keys=["op", "ni"]).dropna()
    if len(both) < 4:
        return out
    last4 = both.sort_index().tail(4)
    if (last4.index[-1] - last4.index[0]).days > TTM_MAX_SPAN_DAYS:
        return out
    if asof is not None and (pd.Timestamp(asof).normalize() - last4.index[-1]).days > MAX_TTM_AGE_DAYS:
        return out
    out.update({"ttm_op": float(last4["op"].sum()), "ttm_ni": float(last4["ni"].sum())})
    return out



def cashflow_fields(cf: pd.DataFrame | None, lf: pd.Timestamp) -> dict:
    """Free cash flow and shareholder payouts for the latest filed year, in
    the REPORTING currency, as positive amounts. Pure.

    Read at the same fiscal year-end as the income statement (`lf`), so
    P/FCF and the shareholder yield are struck on the year the history's
    "today" uses. A year with no cash-flow column is missing, never zero -
    but within a filed column, a payout row Yahoo leaves out (a company that
    paid no dividend has no dividend row) is zero, because the statement
    itself says nothing was paid.
    """
    out = {"lf_fcf": np.nan, "lf_div_paid": np.nan, "lf_buyback": np.nan,
           "lf_issuance": np.nan}
    if cf is None or cf.empty:
        return out
    rows = {k: _row(cf, v) for k, v in CF_ROWS.items()}
    ocf = _num(rows["ocf"].get(lf))
    if not np.isfinite(ocf):
        return out                       # no cash-flow statement for that year
    fcf = _num(rows["fcf"].get(lf))
    if not np.isfinite(fcf):
        capex = _num(rows["capex"].get(lf))
        fcf = ocf + (capex if np.isfinite(capex) else np.nan)

    def paid(key):
        v = _num(rows[key].get(lf))
        return abs(v) if np.isfinite(v) else 0.0
    out.update({"lf_fcf": fcf, "lf_div_paid": paid("div_paid"),
                "lf_buyback": paid("buyback"), "lf_issuance": paid("issuance")})
    return out



def _json_safe(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, (float, np.floating)):
            out[k] = float(v) if np.isfinite(float(v)) else None
        elif isinstance(v, np.integer):
            out[k] = int(v)
        else:
            out[k] = v
    return out


def fetch_statements(snap: pd.DataFrame, cfg: ScreenConfig,
                     asof: str = "") -> pd.DataFrame:
    """Filed statements for the size-gated survivors. Invariant 7: this is the
    slowest work in the run, so it only ever sees names that already cleared
    every cheap filter.

    Three Yahoo calls per ticker (income statement, balance sheet, seven years
    of daily closes) plus one FX history per foreign reporting currency, paced
    like snapshot() and cached per session date for the same reason: a
    throttled statement call returns an empty frame, not an error, and an
    empty frame reads as "no history" - which quietly removes a name from the
    third screen instead of failing loudly.
    """
    import yfinance as yf

    tickers = snap["ticker"].tolist()
    path = os.path.join(cfg.cache_dir,
                        f"statements_v{STATEMENTS_CACHE_VERSION}_{asof or 'latest'}.json")
    cached: dict = {}
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h <= cfg.cache_ttl_hours:
            try:
                with open(path, encoding="utf-8") as fh:
                    cached = json.load(fh)
            except Exception as e:
                log.warning("  statements cache unreadable (%s), refetching", e)

    # FX: one history per reporting currency that differs from the quote's.
    pairs = set()
    for _, r in snap.iterrows():
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy")
        if isinstance(f, str) and f and f != q_major:
            pairs.add((q_major, f))
    fxs: dict = {}
    for q, f in sorted(pairs):
        try:
            h = yf.Ticker(f"{q}{f}=X").history(period="7y", interval="1d")
            s = h["Close"].copy()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            fxs[(q, f)] = s
            log.info("  FX history %s->%s: %d days", q, f, len(s))
        except Exception as e:
            log.warning("  FX history %s->%s failed: %s - those reporters get "
                        "no own-history benchmark", q, f, e)

    todo = [t for t in tickers if t not in cached]
    rows = {t: r for t, r in snap.set_index("ticker").iterrows()}

    guard = RateGuard.from_cfg("statement", cfg)

    def one(t: str):
        if cfg.request_delay:
            time.sleep(cfg.request_delay)
        r = rows[t]
        inc = cf = qinc = h = None
        for attempt in (0, 1):
            if not guard.gate():
                return t, None
            try:
                tk = yf.Ticker(t)
                inc, bs = tk.income_stmt, tk.balance_sheet
                cf = tk.cashflow
                qinc = tk.quarterly_income_stmt
                h = tk.history(period="7y", interval="1d", auto_adjust=False)
            except Exception as e:
                log.debug("%s statements attempt %d: %s", t, attempt, e)
                inc = None
                if is_rate_limit(e):
                    guard.hit()
                continue
            if inc is not None and not inc.empty:
                guard.ok()
                break
            # Empty with no error: a company with no filings, or Yahoo
            # shedding load (the US build's first run: 473 of 2,514).
            # Counted, so a run of them triggers a cooldown; one retry.
            guard.hit()
        if inc is None or inc.empty:
            return t, None
        px = pd.Series(dtype=float)
        if h is not None and not h.empty:
            px = h["Close"].copy()
            px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy") if isinstance(r.get("fin_ccy"), str) else ""
        rec = build_statement_record(
            inc, bs, px, fxs.get((q_major, f)), r.get("currency"), f,
            _num(r.get("close_local")), _num(r.get("market_cap_local")),
            asof=asof or None,
            splits=(h["Stock Splits"] if h is not None and "Stock Splits" in h else None),
            cf=cf, qinc=qinc)
        return t, _json_safe(rec)

    if todo:
        log.info("  statements: fetching %d tickers (%d cached)", len(todo),
                 len(tickers) - len(todo))
        with cf.ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
            for n, (t, rec) in enumerate(ex.map(one, todo), 1):
                if rec is not None:
                    cached[t] = rec
                if n % 50 == 0:
                    log.info("  statements %d/%d", n, len(todo))
        try:
            os.makedirs(cfg.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cached, fh)
        except Exception as e:
            log.warning("  could not write statements cache: %s", e)

    got = [t for t in tickers if t in cached]
    log.info("  statements for %d/%d survivors", len(got), len(tickers))
    if len(got) < 0.8 * len(tickers):
        log.warning("  only %.0f%% have statements - Yahoo is probably "
                    "throttling. The two statement features will be thin; "
                    "re-run to fill the gaps from cache.",
                    100.0 * len(got) / max(len(tickers), 1))
    if not got:
        return pd.DataFrame(columns=["ticker"])
    out = pd.DataFrame([{"ticker": t, **cached[t]} for t in got])
    for k in ("hist_per", "hist_pbr", "hist_evx"):
        if k in out.columns:
            out[k] = out[k].map(lambda v: v if isinstance(v, list) else [])
    # fin_ccy also comes from the snapshot, and the snapshot's is the one kept.
    return out.drop(columns=["fin_ccy"], errors="ignore")
