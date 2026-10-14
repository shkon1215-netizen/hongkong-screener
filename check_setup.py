"""Pre-flight diagnostic: tests every live call the Hong Kong screen makes.

    python check_setup.py

Run this before main_hk.py. Nearly every failure in this build is a SILENT
one - a missing market cap reads as "too small", a broken HKEX feed as "no
pre-gate" (slow, but quiet), a throttled Yahoo as a market where companies
vanished, a currency field Yahoo stops filling as a multiple off by 7.8x -
and a full run is slow enough that finding out afterwards is expensive. Each
check here names what it protects against.
"""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        def run():
            t0 = time.time()
            try:
                detail = fn() or ""
                RESULTS.append((name, True, detail))
                print(f"  OK    {name:<44} {detail}  ({time.time() - t0:.1f}s)")
            except Exception as e:                                # noqa: BLE001
                RESULTS.append((name, False, str(e)))
                print(f"  FAIL  {name:<44} {e}")
        return run
    return deco


@check("HKEX List of Securities")
def roster():
    from config_hk import ScreenConfig
    from providers_hk import HKEXProvider
    p = HKEXProvider(ScreenConfig())
    r = p.listing_roster()
    if len(r) < 2000:
        raise RuntimeError(f"only {len(r)} lines - layout changed?")
    for code in ("00700", "00005", "01398", "09988"):
        if code not in set(r["code"]):
            raise RuntimeError(f"{code} missing from the roster")
    if not (r["trading_ccy"] == "RMB").any():
        raise RuntimeError("no RMB counters - the Trading Currency column moved, and "
                           "second counters would be screened as companies")
    by = r["board"].replace("", "other").value_counts().to_dict()
    return f"{len(r)} lines {by}, as at {p.roster_asof or '?'}"


@check("HKEX equities feed (pre-gate)")
def feed():
    from config_hk import ScreenConfig
    from providers_hk import HKEXProvider
    m = HKEXProvider(ScreenConfig()).market_caps()
    if len(m) < 2000:
        raise RuntimeError(f"only {len(m)} market caps - token or feed moved; the "
                           "run will still work, without the pre-gate (~3x slower)")
    t = m.set_index("code")["hkex_mcap_hkd"].get("00700")
    if not t or t < 1e12:
        raise RuntimeError(f"Tencent's HKEX market cap {t} - units changed?")
    return f"{len(m)} lines, Tencent HK$ {t / 1e9:,.0f}bn"


@check("Yahoo .info on an HK line (rate limit)")
def info():
    import yfinance as yf
    i = yf.Ticker("0700.HK").info
    if not i.get("marketCap") or not i.get("trailingPE"):
        raise RuntimeError("fields missing - Yahoo is throttling this IP; wait and retry")
    if i.get("currency") != "HKD" or i.get("financialCurrency") != "CNY":
        raise RuntimeError(f"currency {i.get('currency')!r} / {i.get('financialCurrency')!r}, "
                           "expected HKD / CNY - the currency repair reads both")
    return f"Tencent HK$ {i['marketCap'] / 1e9:,.0f}bn, P/E {i['trailingPE']:.1f}"


@check("H-share market cap is all shares")
def hshare():
    import yfinance as yf
    i = yf.Ticker("1398.HK").info
    mc, px = i.get("marketCap"), i.get("regularMarketPrice")
    imp = i.get("impliedSharesOutstanding")
    if not (mc and px and imp):
        raise RuntimeError("ICBC fields missing")
    if not 0.9 < mc / (px * imp) < 1.1 or imp < 3e11:
        raise RuntimeError(f"ICBC cap {mc / 1e9:.0f}bn vs price x {imp / 1e9:.0f}bn shares - "
                           "Yahoo changed its H-share basis; the size gate and the "
                           "history screen's basis check both read it")
    return f"ICBC {imp / 1e9:.0f}bn shares (A + H)"


@check("currency split: EV in HKD, EBITDA in USD")
def fxsplit():
    import yfinance as yf
    i = yf.Ticker("1299.HK").info
    ev, mc, eb = i.get("enterpriseValue"), i.get("marketCap"), i.get("ebitda")
    if not (ev and mc and eb):
        raise RuntimeError("AIA EV / cap / EBITDA missing")
    if not 0.5 < ev / mc < 2:
        raise RuntimeError(f"AIA EV/cap {ev / mc:.2f} - EV no longer in HKD")
    return f"AIA EV/EBITDA as Yahoo states it: {ev / eb:.1f} (repaired in the screen)"


@check("market-cap fallback (fast_info)")
def fast():
    import yfinance as yf
    mc = yf.Ticker("0005.HK").fast_info.get("marketCap")
    if not mc or mc < 5e11:
        raise RuntimeError(f"HSBC fast_info market cap {mc} - the fallback is broken")
    return f"HSBC HK$ {mc / 1e9:,.0f}bn"


@check("batched price download (liquidity)")
def download():
    import yfinance as yf
    px = yf.download(["0700.HK", "0005.HK", "1398.HK"], period="30d", interval="1d",
                     progress=False, auto_adjust=False, group_by="column")
    if px.empty or "Volume" not in px.columns.get_level_values(0):
        raise RuntimeError("no Close/Volume panel")
    n = int(px["Volume"].notna().sum().min())
    if n < 10:
        raise RuntimeError(f"only {n} sessions of volume")
    return f"{n} sessions for 3 tickers"


@check("filed statements")
def statements():
    import yfinance as yf
    tk = yf.Ticker("0700.HK")
    inc, bs = tk.income_stmt, tk.balance_sheet
    need_i = ("Total Revenue", "Net Income Common Stockholders")
    need_b = ("Common Stock Equity", "Ordinary Shares Number")
    miss = [k for k in need_i if k not in inc.index] + [k for k in need_b if k not in bs.index]
    if miss:
        raise RuntimeError(f"rows missing {miss} - Yahoo renamed statement lines")
    return f"{inc.shape[1]} columns, normalized income: {'Normalized Income' in inc.index}"


@check("FX HKD->USD and CNY/USD->HKD")
def fx():
    from config_hk import ScreenConfig
    from providers_hk import HKEXProvider
    p = HKEXProvider(ScreenConfig())
    r = p.hkd_to_usd()
    if abs(r - 1 / 7.80) < 1e-12:
        raise RuntimeError("both live sources failed - the peg's midpoint is in use")
    h = p.hkd_per(["CNY", "USD"])
    if not (1.0 < h["CNY"] < 1.2 and 7.7 < h["USD"] < 7.9):
        raise RuntimeError(f"rates {h} out of range")
    return f"{1 / r:.4f} HKD/USD, 1 CNY = {h['CNY']:.4f} HKD"


def main() -> int:
    print("Hong Kong screener pre-flight\n")
    for fn in (roster, feed, info, hshare, fxsplit, fast, download, statements, fx):
        fn()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + ("all checks passed - run python main_hk.py -v"
                  if not bad else f"{len(bad)} check(s) failed: {', '.join(bad)}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
