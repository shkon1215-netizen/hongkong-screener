"""Offline validation of the Hong Kong screener. No network required.

Run: python test_hongkong.py

Every planted case is something a naive screener gets wrong, and most were
measured on the first live run (2026-10-14): Tencent's renminbi counter next
to its Hong Kong dollar one, a temporary "-OLD" counter, Trip.com's secondary
listing, an H-share whose HKEX market cap is an eighth of the company, AIA's
EV/EBITDA of 86.5 (a Hong Kong dollar EV over a US dollar EBITDA), and a
renminbi EPS ratio that only reads right against the year's average rate.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import config_hk as K
import hk_filters as HF
from config_hk import ScreenConfig
from providers_hk import (parse_equity_filter, parse_securities_list, to_yahoo,
                          token_from_page)
from screener import run_screen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

rng = np.random.default_rng(7)

USD_PER_HKD = 1 / 7.80
MCAP = 1.2e10          # ~USD 1.5bn, clears the gate
ADV = 6.0e7            # ~USD 7.7m, clears the gate

FAILURES: list[str] = []


def ok(cond, label):
    print(f"  {'OK  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# Parsers and helpers
# ---------------------------------------------------------------------------
def check_parsers():
    print("=== parsers and helpers ===")
    ok(to_yahoo("00700") == "0700.HK" and to_yahoo("09988") == "9988.HK"
       and to_yahoo("5") == "0005.HK" and to_yahoo("80700") == "80700.HK",
       "HKEX code -> Yahoo symbol keeps four digits at least")
    ok(K.code5(700) == "00700" and K.code5("0005") == "00005", "codes held as five digits")

    n = np.nan
    raw = pd.DataFrame([
        ["List of Securities", n, n, n, n, n, n],
        ["Updated as at 14/10/2026", n, n, n, n, n, n],
        ["Stock Code", "Name of Securities", "Category", "Sub-Category", "ISIN",
         "Spread Table\n1 = Part A\n3 = Part B", "Trading Currency"],
        ["00700", "TENCENT", "Equity", "Equity Securities (Main Board)", "KYG875721634", "1", "HKD"],
        ["80700", "TENCENT-R", "Equity", "Equity Securities (Main Board)", "KYG875721634", "1", "RMB"],
        ["NOTE", "footnote row", n, n, n, n, n],
    ], dtype=object)
    t, asof = parse_securities_list(raw)
    ok(t is not None and len(t) == 2 and asof == "2026-10-14"
       and "Spread Table" in t.columns and "Trading Currency" in t.columns,
       "List of Securities: header found by its cell, multi-line header cut, date read")

    feed = ('cb({"data":{"stocklist":['
            '{"sym":"700","mktcap":"3,813.44","mktcap_u":"B","ccy":"HKD"},'
            '{"sym":"80700","mktcap":"3,813.44","mktcap_u":"B","ccy":"CNY"},'
            '{"sym":"8001","mktcap":"","mktcap_u":"","ccy":"HKD"},'
            '{"sym":"16","mktcap":"316.72","mktcap_u":"B","ccy":"HKD"}]}})')
    m = parse_equity_filter(feed).set_index("code")["hkex_mcap_hkd"]
    ok(abs(m["00700"] - 3.81344e12) < 1e3 and "80700" not in m.index and np.isnan(m["08001"]),
       "HKEX feed: units applied, RMB counter skipped, a blank cap is missing not zero")
    page = ('LabCI.getToken = function () {\n  // ----- THIS IS A SAMPLE -----\n'
            '  //return "Base64-AES-Encrypted-Token";\n'
            '  return "evLtsLsBNAUVTPxtGqVeG%2fsbtUbqiDZV2QvlC6KGo1XG7oNl9U";\n};')
    ok(token_from_page(page).startswith("evLts"), "feed token read, commented sample skipped")

    ok(K.is_secondary_name("TRIP.COM-S") and K.is_secondary_name("JD-SW")
       and not K.is_secondary_name("HKT-SS") and not K.is_secondary_name("BABA-W"),
       "secondary listing by exact suffix: -S / -SW yes, -SS (stapled) and -W no")
    ok(K.is_wvr_name("MEITUAN-W") and K.is_wvr_name("JD-SW") and not K.is_wvr_name("CAPITAL IND-NEW"),
       "weighted voting rights by exact suffix, not any W")
    ok(K.is_temp_counter("BONJOUR H-OLD") and not K.is_temp_counter("BONJOUR H-NEW"),
       "temporary parallel-trading counter is -OLD")
    ok(K.issuer_key("SWIRE PACIFIC A") == K.issuer_key("SWIRE PACIFIC B") == "SWIRE PACIFIC"
       and K.issuer_key("SWIREPROPERTIES") != "SWIRE PACIFIC",
       "Swire Pacific's A and B group as one issuer; Swire Properties stays apart")
    ok(K.issuer_key("BONJOUR H-NEW") == "BONJOUR H", "listing marker removed before grouping")
    ok(K.is_h_share("CNE1000003G1") and not K.is_h_share("KYG875721634"), "H-share by CNE ISIN")
    ok(K.is_financial("Financial Services") and not K.is_financial("Real Estate"),
       "financials by Yahoo's sector; real estate is not one")


# ---------------------------------------------------------------------------
# Roster hygiene and the pre-gate
# ---------------------------------------------------------------------------
def check_roster():
    print("\n=== roster hygiene and pre-gate ===")
    cfg = ScreenConfig()

    def line(code, name, sub="Equity Securities (Main Board)", cat="Equity",
             isin="KYG000000000", ccy="HKD", cap=MCAP):
        return dict(code=code, ticker=to_yahoo(code), symbol=code, name=name, category=cat,
                    subcategory=sub, isin=isin, trading_ccy=ccy, board=K.board_of(sub),
                    hkex_mcap_hkd=cap)
    df = pd.DataFrame([
        line("00700", "TENCENT"),
        line("80700", "TENCENT-R", ccy="RMB"),
        line("02920", "BONJOUR H-OLD"),
        line("00653", "BONJOUR H-NEW", cap=3e8),
        # Share-consolidation counter: same name, temporary code range.
        line("02943", "BILLION IND"),
        line("02299", "BILLION IND"),
        # A permanent code that happens to sit in the range, no twin: kept.
        line("02988", "SOLO LINE"),
        line("01141", "CMBC CAPITAL", sub="Investment Companies"),
        line("00823", "LINK REIT", cat="Real Estate Investment Trusts", sub=""),
        line("09961", "TRIP.COM-S"),
        line("04338", "MICROSOFT-T", sub="Trading Only Securities"),
        line("06288", "FAST RETAIL-DRS", sub="Depositary Receipts"),
        line("06823", "HKT-SS"),
        # PetroChina shape: HKEX's figure is the H tranche, far below the
        # company - it must still go to Yahoo.
        line("00857", "PETROCHINA", isin="CNE1000003W8", cap=3e9),
        line("01234", "SMALL CO", cap=5e8),
        line("05678", "NO FEED CAP", cap=np.nan),
    ])
    out, stats = HF.apply_roster_filters(df, cfg)
    codes = set(out["code"])
    for code, why in [("80700", "renminbi counter dropped - one company, one line"),
                      ("02920", "temporary -OLD counter dropped"),
                      ("02943", "share-consolidation counter (same name, 29xx code) dropped"),
                      ("01141", "Chapter 21 investment company dropped"),
                      ("00823", "REIT dropped by HKEX's category"),
                      ("09961", "secondary listing (-S) dropped"),
                      ("04338", "trading-only US share dropped"),
                      ("06288", "depositary receipt dropped")]:
        ok(code not in codes, why)
    ok({"00700", "00653", "06823", "00857", "02299", "02988"} <= codes,
       "HKD line, permanent -NEW counter, stapled units, H-share, the consolidating "
       "company's own line and a lone 29xx code kept")
    pg, pstats = HF.pregate(out, USD_PER_HKD, cfg)
    pcodes = set(pg["code"])
    ok("00857" in pcodes, "H-share bypasses the pre-gate (HKEX counts the H tranche only)")
    ok("01234" not in pcodes, "a non-H line far below the floor on HKEX's own cap is not sent")
    ok("05678" in pcodes, "a line with no HKEX figure is sent rather than guessed small")

    snap = pd.DataFrame([dict(code="05678", is_h_share=False, market_cap_local=np.nan,
                              hkex_mcap_hkd=9e9, mcap_source=""),
                         dict(code="00857", is_h_share=True, market_cap_local=np.nan,
                              hkex_mcap_hkd=2.2e11, mcap_source="")])
    rep, mstats = HF.repair_market_caps(snap)
    ok(rep.loc[0, "market_cap_local"] == 9e9 and rep.loc[0, "mcap_source"] == "hkex"
       and np.isnan(rep.loc[1, "market_cap_local"]) and mstats["mcap_via_hkex"] == 1,
       "HKEX's cap fills a blank Yahoo cap - never an H-share's (a tranche is not the company)")

    lines = pd.DataFrame([dict(ticker="0019.HK", name="SWIRE PACIFIC A", adv_local=8e7),
                          dict(ticker="0087.HK", name="SWIRE PACIFIC B", adv_local=9e6),
                          dict(ticker="1972.HK", name="SWIREPROPERTIES", adv_local=5e7)])
    kept, lstats = HF.keep_one_line_per_issuer(lines)
    ok(set(kept["ticker"]) == {"0019.HK", "1972.HK"} and lstats["dropped_second_line"] == 1,
       "one line per issuer: Swire's traded A kept, its B dropped, Swire Properties untouched")


# ---------------------------------------------------------------------------
# The screens on a planted universe
# ---------------------------------------------------------------------------
def base(**kw):
    r = dict(code="", name="", isin="KYG000000000", board="MAIN", currency="HKD",
             fin_ccy="HKD", sector="Industrials", industry="Specialty Industrial Machinery",
             quote_type="EQUITY", market_cap_local=MCAP, adv_local=ADV, close_local=50.0,
             trailing_pe=np.nan, price_to_book=np.nan, ev_to_ebitda=np.nan,
             trailing_eps=4.0, book_value_ps=32.0, div_yield=1.5)
    r.update(kw)
    r["ticker"] = to_yahoo(r["code"])
    r["symbol"] = r["code"]
    return r


def make_universe() -> pd.DataFrame:
    rows = []
    specs = [("Specialty Industrial Machinery", "Industrials", 14.0, 1.6, 9.0, 18),
             ("Software - Application", "Technology", 30.0, 4.5, 20.0, 14),
             ("Banks - Regional", "Financial Services", 6.0, 0.6, np.nan, 8)]
    n = 0
    for ind, sec, pe, pb, ev, count in specs:
        for _ in range(count):
            n += 1
            rows.append(base(code=f"{1000 + n:05d}", name=f"PEER {n}", industry=ind, sector=sec,
                             trailing_pe=pe * rng.uniform(0.93, 1.09),
                             price_to_book=pb * rng.uniform(0.93, 1.09),
                             ev_to_ebitda=ev * rng.uniform(0.93, 1.09),
                             market_cap_local=MCAP * rng.uniform(0.9, 2.5)))
    # PASS: genuinely cheap vs machinery peers, good ROE, pays a dividend.
    rows.append(base(code="02001", name="GENUINE VALUE", trailing_pe=7.5, price_to_book=0.75,
                     ev_to_ebitda=4.5, trailing_eps=5.0, book_value_ps=40.0, div_yield=5.0))
    # FAIL: landlord - P/B 0.5 on an appraised book, excluded by Yahoo industry.
    rows.append(base(code="00016", name="SHK PPT", sector="Real Estate",
                     industry="Real Estate - Development", trailing_pe=14.6,
                     price_to_book=0.5, ev_to_ebitda=14.6, trailing_eps=7.5,
                     book_value_ps=218.0, div_yield=3.5))
    # KEEP: property MANAGEMENT is an operating business.
    rows.append(base(code="06098", name="PROPERTY SERVICES", sector="Real Estate",
                     industry="Real Estate Services", trailing_pe=9.0, price_to_book=1.2,
                     ev_to_ebitda=5.0))
    # FAIL (ROE): cheap holdco with 2% ROE - arithmetic, not a discount.
    rows.append(base(code="00142", name="FIRST PACIFIC", trailing_pe=9.0,
                     price_to_book=0.55, ev_to_ebitda=5.5, trailing_eps=0.8,
                     book_value_ps=40.0, div_yield=2.5))
    # FAIL: loss-maker. P/E 0 must read as missing.
    rows.append(base(code="02002", name="LOSS CO", trailing_pe=0.0, price_to_book=0.6,
                     trailing_eps=-2.0, book_value_ps=30.0))
    # A bank: EV/EBITDA suppressed even though given; clears the absolute
    # screen via the P/B + ROE carve-out.
    rows.append(base(code="01398", name="ICBC", isin="CNE1000003G1",
                     sector="Financial Services", industry="Banks - Diversified",
                     trailing_pe=6.7, price_to_book=0.58, ev_to_ebitda=3.0,
                     trailing_eps=1.13, book_value_ps=13.0, div_yield=4.9))
    # Thin industry: one member. Falls back to Yahoo's sector.
    rows.append(base(code="02003", name="LONE MACHINE", industry="Farm & Heavy Construction Machinery",
                     trailing_pe=15.0, price_to_book=1.7, ev_to_ebitda=9.5))
    # FAIL (size).
    rows.append(base(code="02004", name="TINY", market_cap_local=2e9,
                     trailing_pe=5.0, price_to_book=0.5, ev_to_ebitda=3.0))
    return pd.DataFrame(rows)


def check_screens():
    print("\n=== screens ===")
    cfg = ScreenConfig()
    df = make_universe()
    df, ystats = HF.apply_yahoo_filters(df, cfg, {})
    df["market_cap_usd"] = df["market_cap_local"] * USD_PER_HKD
    df = df[df["market_cap_usd"] >= cfg.min_market_cap_usd]
    res, _ = run_screen(df, USD_PER_HKD, cfg)
    res = HF.add_quality_context(res)
    res = HF.add_valueup_flags(res)
    res, _ = HF.apply_roe_gate(res, cfg)
    res, _ = HF.apply_absolute_screen(res, cfg)
    idx = res.set_index("symbol")

    ok("00016" not in idx.index and ystats["dropped_property"] == 1,
       "landlord excluded (HKAS 40 revaluations, appraised book)")
    ok("06098" in idx.index, "property management kept - asset-light, not a landlord")
    ok("02004" not in idx.index, "below the size floor")
    ok(bool(idx.loc["00142", "is_holdco"]) and "Indofood" in idx.loc["00142", "holdco_note"],
       "named holding company tagged with what it holds, not dropped")
    ok(bool(idx.loc["02001", "passes"]), "genuinely cheap name passes the peer screen")
    ok(not bool(idx.loc["00142", "passes_any"]) and idx.loc["00142", "n_metrics_passing"] >= 2,
       "cheap holdco with 2% ROE fails on ROE, not on cheapness")
    ok(pd.isna(idx.loc["02002", "trailing_pe"]), "P/E of 0 read as missing")
    ok(pd.isna(idx.loc["01398", "ev_to_ebitda"]), "bank's EV/EBITDA suppressed (Yahoo sector)")
    ok(bool(idx.loc["01398", "abs_passes"]) and bool(idx.loc["01398", "abs_via_carveout"]),
       "bank clears the absolute screen via the P/B + ROE carve-out")
    ok(idx.loc["02003", "trailing_pe_peer_basis"] == "sector",
       f"thin industry falls back to Yahoo's sector ({idx.loc['02003', 'trailing_pe_peer_basis']!r})")
    ok(idx.loc["02001", "trailing_pe_peer_basis"] == "industry",
       "a populated industry benchmarks within itself")

    hits = res[res["passes_any"]][["symbol", "name", "industry", "trailing_pe",
                                   "price_to_book", "roe_pct", "screen"]]
    print("\n  passing:")
    print(hits.round(2).to_string(index=False) if not hits.empty else "  (none)")


# ---------------------------------------------------------------------------
# The currency repair
# ---------------------------------------------------------------------------
def check_currency():
    print("\n=== currency repair (HKD quote, CNY / USD accounts) ===")
    CNY, CNY_TTM, USD = 1.172, 1.138, 7.848         # HKD per unit: spot, year average
    shares = 1e9
    px = 50.0

    def row(sym, ccy, eps, bps, ni, qeq, ev, ebitda, **kw):
        r = dict(symbol=sym, ticker=sym + ".HK", currency="HKD", fin_ccy=ccy,
                 close_local=px, market_cap_local=px * shares, yf_shares=shares,
                 trailing_eps=eps, trailing_pe=px / eps if eps else np.nan,
                 book_value_ps=bps, price_to_book=px / bps, yf_ni=ni, yf_roe=0.12,
                 yf_ev=ev, yf_ebitda=ebitda, ev_to_ebitda=ev / ebitda,
                 lf_equity=qeq, lf_shares=shares, share_basis_g=1.0)
        r.update(kw)
        return r
    rows = [
        # Tencent shape: EPS converted at the year's AVERAGE rate (k = 1.138),
        # book at spot (k = 1.172), EV in HKD over CNY EBITDA.
        row("TCNT", "CNY", eps=4.0 * CNY_TTM, bps=30.0 * CNY, ni=4.0e9, qeq=30.0e9,
            ev=55e9, ebitda=5e9),
        # The spot-only trap: against spot this EPS ratio (0.97 x spot) sits
        # near the midpoint; against the average it is plainly HKD.
        row("DRFT", "CNY", eps=4.0 * CNY_TTM * 0.995, bps=30.0 * CNY, ni=4.0e9, qeq=30.0e9,
            ev=55e9, ebitda=5e9),
        # A renminbi EPS that looks like the reporting basis: the hypotheses
        # are too close to separate, so it is read as HKD (the dearer reading).
        row("CNYX", "CNY", eps=4.0, bps=30.0 * CNY, ni=4.0e9, qeq=30.0e9,
            ev=55e9, ebitda=5e9),
        # AIA shape: USD reporter, EPS and book in HKD, EV/EBITDA as Yahoo
        # states it is 86.5 - repaired to ~11.
        row("AIAX", "USD", eps=0.78 * USD, bps=4.0 * USD, ni=0.78e9, qeq=4.0e9,
            ev=px * shares * 1.02, ebitda=px * shares * 1.02 / 86.5),
        # A USD reporter whose book Yahoo left in DOLLARS (k = 1, decisive):
        # P/B repaired.
        row("USDB", "USD", eps=0.78 * USD, bps=4.0, ni=0.78e9, qeq=4.0e9,
            ev=55e9, ebitda=5e9 / USD),
        # A rounded EPS (0.01) - evidence of nothing: P/E refused, P/B kept.
        row("RNDX", "CNY", eps=0.01, bps=30.0 * CNY, ni=4.0e9, qeq=30.0e9,
            ev=55e9, ebitda=5e9),
        # A pataca reporter: ~0.97 HKD, too close to tell apart - refused.
        row("MOPX", "MOP", eps=4.0, bps=30.0, ni=4.0e9, qeq=30.0e9, ev=55e9, ebitda=5e9),
        # A Hong Kong dollar reporter: untouched.
        row("HKDX", "HKD", eps=4.0, bps=30.0, ni=4.0e9, qeq=30.0e9, ev=55e9, ebitda=5e9),
        # China Railway shape: EV 8x market cap, outside France's band, but
        # it matches the filed net debt in HKD - repaired, not refused.
        row("SOEX", "CNY", eps=4.0 * CNY_TTM, bps=30.0 * CNY, ni=4.0e9, qeq=30.0e9,
            ev=400e9, ebitda=40e9, lf_debt=330e9 / CNY, lf_cash=10e9 / CNY, lf_mi=30e9 / CNY),
        # The same EV with no filed debt to corroborate it: refused.
        row("BADX", "CNY", eps=4.0 * CNY_TTM, bps=30.0 * CNY, ni=4.0e9, qeq=30.0e9,
            ev=400e9, ebitda=40e9, lf_debt=1e9, lf_cash=1e9, lf_mi=0.0),
        # A bank: no EBITDA, so no EV/EBITDA note and no fx tag.
        row("BNKX", "CNY", eps=4.0 * CNY_TTM, bps=30.0 * CNY, ni=4.0e9, qeq=30.0e9,
            ev=55e9, ebitda=np.nan, sector="Financial Services"),
    ]
    q_equity = {r["ticker"]: r["lf_equity"] for r in rows}
    spot = {"HKD": 1.0, "CNY": CNY, "USD": USD, "MOP": 0.971}
    ttm = {"HKD": 1.0, "CNY": CNY_TTM, "USD": USD, "MOP": 0.971}
    df, stats = HF.repair_foreign_reporters(pd.DataFrame(rows), spot, ttm, q_equity)
    i = df.set_index("symbol")
    ok(abs(i.loc["TCNT", "trailing_pe"] - px / (4.0 * CNY_TTM)) < 1e-9
       and abs(i.loc["TCNT", "price_to_book"] - px / (30.0 * CNY)) < 1e-9,
       "Tencent shape: HKD EPS and book left alone")
    ok(abs(i.loc["TCNT", "ev_to_ebitda"] - 55e9 / (5e9 * CNY_TTM)) < 1e-9,
       f"HKD EV over CNY EBITDA repaired at the average rate ({i.loc['TCNT', 'ev_to_ebitda']:.2f})")
    ok(pd.notna(i.loc["DRFT", "trailing_pe"]) and i.loc["DRFT", "fx_note"].find("P/E") < 0,
       "EPS tested against the year's average rate, not spot")
    ok(abs(i.loc["CNYX", "trailing_pe"] - px / 4.0) < 1e-9,
       "renminbi too close to call: never 'repaired' into a cheaper P/E")
    ok(abs(i.loc["AIAX", "ev_to_ebitda"] - 86.5 / USD) < 1e-6
       and abs(i.loc["AIAX", "price_to_book"] - px / (4.0 * USD)) < 1e-9,
       f"AIA shape: EV/EBITDA 86.5 -> {i.loc['AIAX', 'ev_to_ebitda']:.2f}, P/B left alone")
    ok(abs(i.loc["USDB", "price_to_book"] - px / (4.0 * USD)) < 1e-9
       and "P/B repaired" in i.loc["USDB", "fx_note"],
       "dollar book value on a decisive currency: P/B repaired")
    ok(np.isnan(i.loc["RNDX", "trailing_pe"]) and pd.notna(i.loc["RNDX", "price_to_book"]),
       "a rounded EPS refuses the P/E only")
    ok(i.loc["MOPX", ["trailing_pe", "price_to_book", "ev_to_ebitda"]].isna().all()
       and i.loc["MOPX", "fx_note"] == "fx basis untestable",
       "a currency near 1 HKD cannot be tested -> refused, not guessed")
    ok(i.loc["HKDX", "price_to_book"] == px / 30.0 and i.loc["HKDX", "fx_note"] == "",
       "HKD reporter untouched")
    ok(abs(i.loc["SOEX", "ev_to_ebitda"] - 400e9 / (40e9 * CNY_TTM)) < 1e-9,
       "indebted SOE: an EV of 8x market cap corroborated by filed debt is kept")
    ok(np.isnan(i.loc["BADX", "ev_to_ebitda"]) and "EV/EBITDA refused" in i.loc["BADX", "fx_note"],
       "the same EV with nothing to corroborate it is refused")
    ok(i.loc["BNKX", "fx_note"] == "", "a bank earns no EV/EBITDA note (it has none to repair)")
    ok(stats["fx_pb_repaired"] == 1 and stats["fx_pe_refused"] == 1
       and stats["fx_eps_on_reporting_basis"] == 0,
       f"funnel counts the repair ({stats})")


# ---------------------------------------------------------------------------
# Pyramids
# ---------------------------------------------------------------------------
def check_pyramids():
    print("\n=== pyramids ===")
    cfg = ScreenConfig()

    def row(name, rev, op, eq, mi, ccy="HKD", fy="2023,2024,2025", sector="Real Estate"):
        return dict(name=name, ticker=name[:4] + ".HK", rev_y3=rev, op_y3=op,
                    lf_equity=eq, lf_mi=mi, fin_ccy=ccy, fin_years=fy, is_holdco=False,
                    sector=sector)
    df = pd.DataFrame([
        # Hang Lung Group / Hang Lung Properties: one income statement; the
        # group's equity is largely Hang Lung Properties' other holders.
        row("HANG LUNG PPT", 11050.0, 7200.0, 130e9, 3e9),
        row("HANG LUNG GROUP", 11061.0, 7190.0, 85e9, 70e9),
        # The first live run's false pair: a coal miner with heavy minorities
        # and a hog farmer, revenue and operating profit within tolerance.
        row("YANKUANG ENERGY", 144933.0, 20100.0, 90e9, 60e9, ccy="CNY", sector="Energy"),
        row("MUYUAN", 144145.0, 20500.0, 110e9, 5e9, ccy="CNY", sector="Consumer Defensive"),
        # Same revenue by chance, nothing else in common.
        row("COINCIDENCE A", 5639.0, 900.0, 4.0e9, 0.3e9),
        row("COINCIDENCE B", 5648.0, 450.0, 2.5e9, 0.05e9),
    ])
    out, stats = HF.flag_pyramids(df, cfg)
    names = set(out["name"])
    ok(stats["pyramid_pairs"] == 1 and "HANG LUNG GROUP" not in names,
       f"parent consolidating a listed subsidiary dropped ({stats})")
    ok(out.set_index("name").loc["HANG LUNG PPT", "pyramid_child_of"] == "HANG LUNG GROUP",
       "subsidiary tagged with its listed parent")
    ok({"COINCIDENCE A", "COINCIDENCE B"} <= names, "a revenue coincidence is not a pyramid")
    ok({"YANKUANG ENERGY", "MUYUAN"} <= names,
       "coal miner and hog farmer with matching statements: different sectors, not a pyramid")


# ---------------------------------------------------------------------------
# Filed statements, splits and the history screen (the shared code)
# ---------------------------------------------------------------------------
YEARS = pd.to_datetime(["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"])


def stmt(rows: dict) -> pd.DataFrame:
    # Yahoo shape: line items as rows, newest year first, oldest column empty.
    df = pd.DataFrame({k: [np.nan] + list(v) for k, v in rows.items()}, index=YEARS).T
    return df[df.columns[::-1]]


def company(ni=(100e6, 110e6, 120e6, 130e6), shares=(1e8,) * 4):
    inc = stmt({"Total Revenue": (1000e6, 1100e6, 1200e6, 1300e6),
                "Operating Income": (150e6, 160e6, 170e6, 180e6),
                "EBITDA": (200e6, 210e6, 220e6, 230e6),
                "Net Income Common Stockholders": ni})
    bs = stmt({"Common Stock Equity": (1000e6,) * 4, "Ordinary Shares Number": shares,
               "Total Debt": (300e6,) * 4, "Cash And Cash Equivalents": (100e6,) * 4})
    return inc, bs


def check_statements():
    from providers_hk import build_statement_record, cagr
    print("\n=== filed statements ===")
    px = pd.Series(20.0, index=pd.date_range("2020-01-01", "2026-10-14", freq="B"))

    inc, bs = company()
    rec = build_statement_record(inc, bs, px, None, "HKD", "HKD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_years"] == "2022,2023,2024,2025", "Yahoo's empty padded year is not a filed year")
    ok(rec["hist_pbr"] == [2.0] * 4, f"P/B history in HKD ({rec['hist_pbr']})")
    ok(rec["fin_years"] == "2023,2024,2025" and rec["rev_y3"] == 1300.0,
       "3-year history is the last three filed years, in millions")
    ok(np.isnan(cagr([-50, 10, 20])), "CAGR undefined on a negative base")

    # A renminbi reporter quoted in HKD - the Tencent shape. HK$2bn at 0.85
    # CNY per HKD is CNY 1.7bn over CNY 1bn of equity: P/B 1.7.
    fx = pd.Series(0.85, index=px.index)
    rec = build_statement_record(inc, bs, px, fx, "HKD", "CNY", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_pbr"] == [1.7] * 4, f"CNY reporter converted at each year-end ({rec['hist_pbr']})")
    rec = build_statement_record(inc, bs, px, None, "HKD", "CNY", close_now=20.0, mcap_now=2e9)
    ok(all(v is None for v in rec["hist_pbr"]), "CNY reporter without FX -> no benchmark")

    # H-share: Yahoo's cap is the H price x ALL shares (A + H), and so is the
    # filed count - the basis check must pass. 100m x HK$20 = HK$2bn.
    rec = build_statement_record(inc, bs, px, None, "HKD", "HKD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_note"] == "", "H-share: all-shares market cap matches the filed count")
    # Had the cap been the H tranche only (a quarter of the shares), the
    # basis check refuses rather than inventing a 4x premium.
    rec = build_statement_record(inc, bs, px, None, "HKD", "HKD", close_now=20.0, mcap_now=0.5e9)
    ok(rec["hist_note"] == "price/share basis mismatch",
       "H-tranche-only market cap refused by the basis check")

    inc2, bs2 = company(shares=(1e7, 1e7, 1e8, 1e8))
    rec = build_statement_record(inc2, bs2, px, None, "HKD", "HKD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_note"] == "share-count break" and rec["hist_pbr"] == [],
       "share split between filings -> no history, not a 90% 'premium'")

    inc4, bs4 = company(ni=(100e6, 100e6, 100e6, 200e6))
    inc4.loc["Normalized Income"] = inc4.loc["Net Income Common Stockholders"]
    inc4.loc["Normalized Income", YEARS[-1]] = 100e6
    inc4.loc["Normalized EBITDA"] = inc4.loc["EBITDA"]
    rec = build_statement_record(inc4, bs4, px, None, "HKD", "HKD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_earnings"] == "normalized" and rec["hist_per"][-1] == 20.0,
       "one-off gain: valuation on normalized earnings")


def check_split_basis():
    from providers_hk import build_statement_record
    print("\n=== splits since the latest filing ===")
    px = pd.Series(20.0, index=pd.date_range("2020-01-01", "2026-10-14", freq="B"))
    splits = lambda ratio: pd.Series([ratio], index=[pd.Timestamp("2026-08-03")])  # noqa: E731

    inc, bs = company(shares=(100e6,) * 4)
    rec = build_statement_record(inc, bs, px, None, "HKD", "HKD", close_now=20.0,
                                 mcap_now=20.0 * 75e6, splits=splits(0.75))
    ok(rec["hist_note"] == "" and abs(rec["share_basis_g"] - 0.75) < 1e-9
       and rec["hist_pbr"] == [1.5] * 4,
       f"unrestated consolidation: history restated by 0.75 ({rec['hist_pbr']})")
    inc, bs = company(shares=(20e6,) * 4)
    rec = build_statement_record(inc, bs, px, None, "HKD", "HKD", close_now=20.0,
                                 mcap_now=20.0 * 100e6, splits=None)
    ok(rec["hist_note"] == "price/share basis mismatch" and rec["hist_pbr"] == [],
       "unexplained share-count gap refused, not guessed")

    df = pd.DataFrame([
        dict(share_basis_g=0.75, lf_equity=1000e6, lf_shares=100e6, book_value_ps=10.0,
             trailing_eps=1.0, trailing_pe=20.0, price_to_book=2.0, div_yield=4.0),
        dict(share_basis_g=1.0, lf_equity=1000e6, lf_shares=100e6, book_value_ps=10.0,
             trailing_eps=1.0, trailing_pe=20.0, price_to_book=2.0, div_yield=4.0),
    ])
    out, _ = HF.restate_info_for_splits(df)
    ok(abs(out.loc[0, "price_to_book"] - 1.5) < 1e-9 and out.loc[1, "price_to_book"] == 2.0,
       ".info on the old basis restated; no split, untouched")


def check_history_screen():
    print("\n=== own-history screen ===")
    cfg = ScreenConfig()

    def row(sym, pers, pbrs, evxs, sector="Industrials", roe_ok=True):
        return dict(symbol=sym, ticker=sym + ".HK", sector=sector, industry="x",
                    hist_per=pers, hist_pbr=pbrs, hist_evx=evxs,
                    market_cap_local=1.0e9, fx_now=1.0, lf_ni=100e6,
                    lf_equity=1000e6, lf_ebitda=200e6, lf_debt=300e6,
                    lf_cash=100e6, lf_mi=0.0, passes=False, abs_passes=False,
                    roe_ok=roe_ok, avg_discount=0.0, hist_note="")
    h = ([20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10])
    df = pd.DataFrame([row("DRTD", *h), row("LOWR", *h, roe_ok=False),
                       row("THIN", [20, None, None, 21], [2.0, None, None, 2.1],
                           [10, None, None, 11]),
                       row("BANK", *h, sector="Financial Services"),
                       row("PROP", *h, sector="Real Estate")])
    res, _ = HF.apply_history_screen(df, cfg)
    idx = res.set_index("symbol")
    d = idx.loc["DRTD"]
    ok(bool(d["hist_passes"]) and d["screen"] == "history", "de-rated name passes on history alone")
    ok(abs(d["per_now"] - 10.0) < 1e-9, "today measured on the history's basis")
    ok(not bool(idx.loc["LOWR", "hist_passes"]), "ROE floor applies to the history screen")
    ok(idx.loc["THIN", "hist_n_valid"] == 0, "fewer than 3 usable years -> no benchmark")
    ok(pd.isna(idx.loc["BANK", "hist_evx_med"]) and pd.isna(idx.loc["BANK", "evx_now"]),
       "financials: EV/EBITDA skipped in history and today")
    ok(pd.notna(idx.loc["PROP", "evx_now"]), "real estate keeps EV/EBITDA (not a financial)")


def main() -> int:
    check_parsers()
    check_roster()
    check_screens()
    check_currency()
    check_pyramids()
    check_statements()
    check_split_basis()
    check_history_screen()
    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
