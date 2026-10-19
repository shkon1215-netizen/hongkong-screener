"""Configuration for the Hong Kong (HKEX) valuation screener.

Seventh market after Korea, the UK, Japan, Germany, Poland and France, built
on France's copy because France had already combined what the others
learned. The quality floor, the discount test and the history screen are the
user's screening preferences and are carried over unchanged. What is local:

  * the roster - HKEX's own List of Securities, which names every line's
    category (equity, REIT, investment company, depositary receipt), its
    board (Main Board or GEM) and its trading currency, so most hygiene is an
    exact lookup, as in Germany and France
  * the pre-gate - HKEX's equities feed carries a market cap for every line
    in one request, so Yahoo's slow .info runs on ~1,200 lines, not 2,800
    (invariant 7, the Korea / Japan reading). H-shares are exempt: HKEX's
    figure for them is the H tranche only
  * classification - Yahoo's industry, falling back to Yahoo's sector, as in
    the UK. HKEX files every company under the Hang Seng Industry
    Classification but publishes it only line by line on its web pages; no
    free whole-market file exists
  * the currency trap - France's, at Hong Kong's scale: most of the market by
    value reports in renminbi or dollars and quotes in Hong Kong dollars
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# P/FCF is the fourth: market value over the last filed free cash flow
# (add_cashflow_metrics). The relative test is unchanged - 20% below
# peers on at least two of the metrics a company has.
VALUATION_METRICS = ("trailing_pe", "price_to_book", "ev_to_ebitda", "price_to_fcf")

METRIC_LABELS = {
    "trailing_pe": "P/E",
    "price_to_book": "P/B",
    "ev_to_ebitda": "EV/EBITDA",
    "price_to_fcf": "P/FCF",
}

# Same reasoning as Korea: a zero or negative multiple means "no earnings" or
# "negative equity", never "cheap". Ping An's EV comes back negative from
# Yahoo (an insurer's cash exceeds its debt plus market cap), so its
# EV/EBITDA is -0.84 - the lower bound is what removes it.
METRIC_BOUNDS = {
    "trailing_pe": (1.0, 200.0),
    "price_to_book": (0.05, 30.0),
    "ev_to_ebitda": (0.5, 100.0),
    # P/E's bounds: a free cash flow is an earnings-like flow.
    "price_to_fcf": (1.0, 200.0),
}

# ---------------------------------------------------------------------------
# HKEX's List of Securities
# ---------------------------------------------------------------------------
# Only the Equity category is read. REITs are their own category and are
# counted, then excluded (see is_reit). Warrants, CBBCs, debt and ETPs never
# enter.
EQUITY_CATEGORY = "Equity"
REIT_CATEGORY = "Real Estate Investment Trusts"
BOARDS = {"Equity Securities (Main Board)": "MAIN",
          "Equity Securities (GEM)": "GEM"}
# Chapter 21 investment companies: closed-end funds listed as equity, priced
# at a standing discount to NAV - Hong Kong's investment trusts.
INVESTMENT_SUBCATEGORY = "Investment Companies"
# Depositary receipts and trading-only securities have their primary market
# elsewhere; HKEX names the class outright.
FOREIGN_SUBCATEGORIES = ("Depositary Receipts", "Trading Only Securities")
# Only the Hong Kong dollar counter of a company is screened. HKEX's dual
# counter model gives two dozen companies a second line in renminbi (Tencent
# 00700 and 80700) - same shares, same earnings, a second price in another
# currency. The list says which is which.
HOME_CURRENCY = "HKD"

# Name markers HKEX appends to a short name (Listing Rules 8A and 19C):
#   -W   weighted voting rights            BABA-W, MEITUAN-W, XIAOMI-W
#   -S   secondary listing                 TRIP.COM-S, MANULIFE-S, AUTOHOME-S
#   -SW  both                              JD-SW, NIO-SW
#   -B   pre-revenue biotech (Ch. 18A)     loss-makers; the multiples sanitize
#   -P   pre-commercial specialist tech (Ch. 18C)
# A secondary listing is Germany's foreign secondary line: the price is set
# in New York or Toronto and Hong Kong follows, and Yahoo's per-share figures
# for these lines are the ADS's in places. The dual-primary converts (BABA-W,
# BIDU-W, XPENG-W) dropped the S when they converted, so the marker is exact.
#
# Exact suffixes, not patterns: "-SS" is a share-stapled unit (HKT, HK
# Electric, Langham), "-NEW" a permanent counter during a board-lot change.
SECONDARY_MARK = re.compile(r"-(S|SW)$")
WVR_MARK = re.compile(r"-(W|SW)$")
# A parallel-trading counter: while a company changes its board lot, HKEX
# opens a temporary line (codes 29xx) named "-OLD" beside the permanent one,
# named "-NEW" (BONJOUR H-OLD 02920, BONJOUR H-NEW 00653) - one company, two
# prices, for a few weeks.
TEMP_COUNTER_MARK = re.compile(r"-OLD$")
# The other temporary counter: during a share consolidation HKEX opens a
# second line under the SAME short name in its temporary code ranges -
# BILLION IND 02943 beside 02299, CAPITAL FIN 08585 beside 08239 (nine pairs
# on the 2026-10-14 list). Its market cap is built on the wrong share count
# for weeks (HKEX shows the two lines of SG Group 10x apart), so it is
# dropped by the roster, not left for the one-line rule to sort out.
TEMP_COUNTER_CODES = ((2900, 2999), (8500, 8599))

# ---------------------------------------------------------------------------
# Yahoo classification
# ---------------------------------------------------------------------------
# Financials, by Yahoo's sector: banks, insurers, brokers, asset managers and
# HKEX itself. Yahoo gives real estate its own sector, so no carve-out is
# needed for invariant 6.
FINANCIAL_SECTORS = ("Financial Services",)

# Property. Hong Kong's landlords carry their buildings at fair value under
# HKAS 40, so every revaluation runs through the income statement - Germany's
# reason for excluding its landlords. Yahoo files Sun Hung Kai Properties
# (P/B 0.50, ROE 3.5%) and China's developers in one industry, "Real Estate -
# Development", and HK landlords also sit in "Real Estate - Diversified".
# Measured 2026-10-14: 28 of them clear USD 600m, median P/B 0.33 (New World
# 0.09, Longfor 0.18, CK Asset 0.39), and 9 of the largest 20 have no P/E at
# all - Germany's revaluations ran through earnings as gains, Hong Kong's
# are running as losses. Either way the P/E is the appraiser's year and the
# P/B an appraised book, and left in nearly all 28 would join the 183 names
# the absolute screen finds below book. Property MANAGEMENT ("Real Estate Services") is an
# asset-light operating business and stays.
PROPERTY_INDUSTRIES = ("Real Estate - Development", "Real Estate - Diversified")

# ---------------------------------------------------------------------------
# Holding companies - flags by default, never silent
# ---------------------------------------------------------------------------
# Nearly every Hong Kong company is called "... HOLDINGS", so a name token
# says nothing here (it would tag the whole market). France's answer: a named
# list, by stock code, with what each holds, plus the data test for a parent
# that consolidates a listed subsidiary (hk_filters.flag_pyramids).
KNOWN_HOLDCOS = {
    "00019": "Swire Pacific (Swire Properties ~82%, Cathay Pacific ~45%)",
    "00087": "Swire Pacific B (Swire Properties ~82%, Cathay Pacific ~45%)",
    "00142": "First Pacific (Indofood, PLDT, Metro Pacific, Philex)",
    "00010": "Hang Lung Group (Hang Lung Properties)",
    "00053": "Guoco Group (Hong Leong group stakes)",
}

# A parent consolidating a listed subsidiary files the subsidiary's income
# statement plus its own small activities. France's thresholds, unchanged.
PYRAMID_REVENUE_TOL = 0.01
PYRAMID_OP_TOL = 0.05
PYRAMID_MIN_MI_SHARE = 0.25
PYRAMID_MI_GAP = 0.15

# Closely held: Yahoo's heldPercentInsiders at a majority. In Hong Kong that
# is the founding family (Sun Hung Kai Properties 55%, the Kwoks) or a parent
# (China Mobile 72%, China Mobile Communications Group). France's flag,
# unchanged in meaning: the free float is the minority. For H-shares Yahoo
# counts holders of the H tranche only, so the state's A-share stake does not
# reach it - see the `h` tag instead.
CONTROL_MIN_INSIDERS = 0.50

# H-shares: companies incorporated in mainland China, which HKEX lists with a
# CNE ISIN. Their Hong Kong line is one tranche of the company; most also
# trade A-shares in Shanghai or Shenzhen, usually at a premium.
H_SHARE_ISIN_PREFIX = "CNE"


@dataclass
class ScreenConfig:
    # --- Size / liquidity, specified in USD then converted at live FX ---
    min_market_cap_usd: float = 600_000_000
    # Measured, not inherited - see CLAUDE.md "The liquidity gate". HKEX is
    # the only venue, so this is the whole market's volume. At the
    # cross-market USD 4m the screen loses CRCC, CCCC, Zoomlion, Shanghai
    # Pharma, CIMC, Sinotrans - USD 5-9bn companies whose H-shares trade
    # 1-4m a day while their A-shares carry the volume. Under 1m goes what
    # should: H-share banks that do not trade at all, RUSAL, Guoco, and GEM
    # lines whose market cap a share consolidation has corrupted. France's
    # and Poland's finding, and their number.
    min_adv_usd: float = 1_000_000
    adv_lookback_days: int = 60                 # trading days
    # The HKEX pre-gate: lines whose whole Hong Kong market cap is under this
    # share of the floor are not sent to Yahoo at all. Not the floor itself:
    # HKEX's figure and Yahoo's differ by a day's price and a share count, and
    # the gate proper is still Yahoo's. H-shares bypass it.
    pregate_fraction: float = 0.25
    # The liquidity pre-gate (hk_filters.adv_pregate): the batched price
    # download runs over every roster line BEFORE .info, and a line under
    # min_adv_usd is not sent - it could never pass the liquidity gate,
    # which reads the same panel. It does not depend on HKEX's feed, which
    # went dead around 2026-10-15 (26 rows for every query). Off with
    # --no-pregate and --skip-liquidity.
    adv_pregate: bool = True
    # Share of the liquid lines (those the ADV pre-gate passed on measured
    # turnover) that Yahoo must price, or the run refuses to screen. Every
    # one of them traded at least min_adv_usd a day over the lookback, so a
    # miss is throttling, not a dead line. 0.5 when the ADV pre-gate is off.
    min_priced_share: float = 0.9

    # --- Valuation test ---
    discount_threshold: float = 0.20
    metrics: tuple[str, ...] = VALUATION_METRICS
    min_metrics_passing: int = 2
    min_valid_metrics: int = 2

    # --- Quality floor ---
    min_roe_pct: float = 5.0
    roe_good_pct: float = 10.0

    # --- Absolute value screen ---
    abs_max_pbr: float = 1.0
    abs_max_ev_ebitda: float = 8.0
    abs_require_roe: bool = True
    abs_financials_pbr_only: bool = True
    abs_require_pbr_vs_roe: bool = True
    abs_cost_of_equity_pct: float = 10.0
    # The user's 2% payout floor, now on the SHAREHOLDER yield (dividends +
    # buybacks - issuance); the dividend yield where no cash flow was filed.
    abs_min_div_yield: float = 2.0

    # --- One-off guard (apply_oneoff_guard) ---
    oneoff_guard: bool = True
    # Trailing net income above this multiple of after-tax operating profit
    # is treated as below-the-line. Ordinary companies measure 1.0-1.2.
    oneoff_ratio: float = 1.5
    # Profits tax; mainland companies pay 25%, so this is lenient for them.
    oneoff_tax_pct: float = 16.5

    # --- Own-history screen ---
    hist_min_discount: float = 0.30
    hist_min_metrics: int = 2
    hist_min_years: int = 3
    hist_require_roe: bool = True

    # --- Peer groups ---
    # industry = Yahoo industry, sector = Yahoo sector (the UK's shape).
    peer_keys: tuple[str, ...] = ("industry",)
    fallback_peer_keys: tuple[str, ...] = ("sector",)
    min_peers: int = 5
    winsor_pct: float = 0.05

    # --- Hong Kong universe hygiene ---
    exclude_investment_companies: bool = True
    exclude_reits: bool = True
    exclude_property: bool = True           # HKAS 40 landlords and developers
    exclude_secondary: bool = True          # -S lines, DRs, trading-only securities
    exclude_pyramid_parents: bool = True    # one set of earnings, twice
    flag_holdcos: bool = True
    exclude_holdcos: bool = False           # flagged by default, not dropped
    exclude_controlled: bool = False

    exclude_sectors: tuple[str, ...] = ()

    # --- Fetching ---
    # Yahoo's .info is the call that throttles, globally and silently - the
    # UK build's measured setting.
    max_workers: int = 2
    request_delay: float = 0.25
    # providers_hk.RateGuard: this many failed requests in a row (a
    # rate-limit error, a .info with no price and no market cap, an empty
    # income statement) pause every worker for the cooldown, up to the
    # number of cooldowns; then the pool stops and the run refuses to screen.
    guard_limit: int = 25
    guard_cooldown_s: float = 120.0
    guard_max_cooldowns: int = 3
    # Lines still unpriced after the pool (lost to a shedding stretch before
    # the guard paused) are asked once more, after this pause, by a fresh
    # pool - so a run recovers them itself instead of needing a re-run.
    # Measured 2026-10-19: the first 25 .info answers came back empty on two
    # cold runs, one of them after a 60s rest following the price download,
    # so the download is not the cause and no rest is taken after it.
    snapshot_second_pass_pause_s: float = 60.0
    cache_dir: str = ".hk_cache"
    cache_ttl_hours: int = 20


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
REIT_TOKENS = ("REIT",)

# Words dropped from HKEX short names before lines are grouped by issuer:
# Swire Pacific lists "SWIRE PACIFIC A" and "SWIRE PACIFIC B".
CLASS_WORDS = {"A", "B"}


def plain(s: str) -> str:
    """Upper-case, punctuation to spaces."""
    return " ".join(re.sub(r"[^A-Z0-9&]+", " ", str(s or "").upper()).split())


def code5(code) -> str:
    """HKEX stock code, five digits: 700 -> '00700'."""
    s = re.sub(r"\D", "", str(code or ""))
    return s.zfill(5) if s else ""


def issuer_key(name: str) -> str:
    """Groups the listed lines of one issuer. HKEX's short names are the
    whole key - Swire's A and B ISINs share nothing (HK0019000162,
    HK0087000532). The listing markers (-W, -B) go first, then the trailing
    class letter, Germany's Draegerwerk lesson about order."""
    n = re.sub(r"-[A-Z]{1,3}$", "", str(name or "").upper().strip())
    toks = plain(n).split()
    while toks and toks[-1] in CLASS_WORDS:
        toks = toks[:-1]
    return " ".join(toks)


def board_of(subcategory: str) -> str:
    return BOARDS.get(str(subcategory or "").strip(), "")


def is_secondary_name(name: str) -> bool:
    return bool(SECONDARY_MARK.search(str(name or "").strip().upper()))


def is_temp_counter(name: str) -> bool:
    return bool(TEMP_COUNTER_MARK.search(str(name or "").strip().upper()))


def in_temp_range(code) -> bool:
    c = code5(code)
    return bool(c) and any(lo <= int(c) <= hi for lo, hi in TEMP_COUNTER_CODES)


def is_wvr_name(name: str) -> bool:
    return bool(WVR_MARK.search(str(name or "").strip().upper()))


def is_h_share(isin: str) -> bool:
    return str(isin or "").upper().startswith(H_SHARE_ISIN_PREFIX)


def is_financial(sector: str = "", industry: str = "") -> bool:
    """Yahoo's Financial Services sector. Called (sector, industry) by the
    dashboard too, so its carve-out agrees with Python's."""
    return str(sector or "").strip() in FINANCIAL_SECTORS


def is_property(industry: str) -> bool:
    return str(industry or "").strip() in PROPERTY_INDUSTRIES


def is_reit(name: str, industry: str = "") -> bool:
    return ("REIT" in str(industry or "").upper()
            or any(t in plain(name).split() for t in REIT_TOKENS))


def is_holdco(code: str) -> bool:
    return code5(code) in KNOWN_HOLDCOS
