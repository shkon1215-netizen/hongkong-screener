"""Hong Kong-specific filters layered on top of the generic screener.

Everything from add_valueup_flags down is France's, which is Poland's, which
is Germany's, which is the UK's, which is Korea's: the quality floor, the
absolute screen and the own-history screen are not country-specific. What is
local, and which earlier market each piece comes from:

  * roster hygiene - all exact lookups on HKEX's own list: second counters
    in renminbi (Germany's one-line rule, decided by the exchange's currency
    field), temporary parallel-trading counters, Chapter 21 investment
    companies (Germany's PE vehicles, Poland's sector 180), REITs (every
    market), and secondary listings, depositary receipts and trading-only
    securities (Germany's foreign secondaries, France's home-venue rule)
  * Yahoo hygiene - landlords and developers (Germany's IAS 40 reasoning),
    REITs Yahoo names that HKEX files as equity, fund quotes
  * one line per issuer - Germany's, for Swire Pacific's A and B shares
  * holding companies - France's named list plus its pyramid test
  * control - France's closely-held flag, from Yahoo's insider holding
  * the currency repair - France's, which matters far more here
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config_hk as K

log = logging.getLogger(__name__)


def _fin_mask(df: pd.DataFrame) -> pd.Series:
    """Banks, insurers, brokers and asset managers, by Yahoo's sector. Yahoo
    gives real estate its own sector, so no carve-out is needed."""
    sec = df.get("sector", pd.Series("", index=df.index)).fillna("").astype(str)
    return sec.map(K.is_financial).astype(bool)


# ---------------------------------------------------------------------------
# Universe hygiene
# ---------------------------------------------------------------------------
def tag_roster(df: pd.DataFrame) -> pd.DataFrame:
    """Flags HKEX's list decides on its own. Pure."""
    df = df.copy()
    name = df["name"].fillna("").astype(str)
    sub = df.get("subcategory", pd.Series("", index=df.index)).fillna("").astype(str)
    cat = df.get("category", pd.Series("", index=df.index)).fillna("").astype(str)
    ccy = df.get("trading_ccy", pd.Series(K.HOME_CURRENCY, index=df.index)) \
            .fillna("").astype(str)
    isin = df.get("isin", pd.Series("", index=df.index)).fillna("").astype(str)
    df["is_other_counter"] = ccy.ne(K.HOME_CURRENCY)
    # "-OLD" by name, or a second line under an identical name in HKEX's
    # temporary code ranges (config_hk.TEMP_COUNTER_CODES).
    code = df.get("code", pd.Series("", index=df.index)).fillna("").astype(str)
    twin = name.str.upper().str.strip().duplicated(keep=False) & ccy.eq(K.HOME_CURRENCY)
    df["is_temp_counter"] = name.map(K.is_temp_counter) | (twin & code.map(K.in_temp_range))
    df["is_investment_co"] = sub.eq(K.INVESTMENT_SUBCATEGORY)
    df["is_reit"] = cat.eq(K.REIT_CATEGORY) | name.map(K.is_reit)
    df["is_foreign_secondary"] = sub.isin(K.FOREIGN_SUBCATEGORIES) | name.map(K.is_secondary_name)
    df["is_h_share"] = isin.map(K.is_h_share)
    df["is_wvr"] = name.map(K.is_wvr_name)
    return df


def apply_roster_filters(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """What HKEX's list alone can decide, before a single Yahoo call.

    Every one of these is the exchange's own label, so none is a heuristic:

      second counter      Tencent trades as 00700 in HKD and 80700 in RMB;
                          only the HKD counter is screened
      temporary counter   "-OLD" lines opened for a board-lot change
      investment company  Chapter 21 closed-end funds - a standing discount
                          to NAV, Hong Kong's investment trusts
      REIT                HKEX's own category (Link, Champion, Fortune)
      secondary listing   "-S" names (Trip.com, Manulife, Autohome), whose
                          price is set in New York or Toronto, plus HKEX's
                          depositary receipts and trading-only US shares
    """
    df = tag_roster(df)
    stats = {"listings": len(df)}

    def drop(flag: str, key: str, on: bool):
        nonlocal df
        if on:
            stats[key] = int(df[flag].sum())
            df = df[~df[flag]]

    drop("is_other_counter", "dropped_other_counter", True)
    drop("is_temp_counter", "dropped_temp_counter", True)
    drop("is_investment_co", "dropped_investment_co", cfg.exclude_investment_companies)
    drop("is_reit", "dropped_reit", cfg.exclude_reits)
    drop("is_foreign_secondary", "dropped_foreign_secondary", cfg.exclude_secondary)
    return df.copy(), stats


def pregate(df: pd.DataFrame, usd_per_hkd: float,
            cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """HKEX's market cap as a cheap first cut, so Yahoo's throttled .info
    only sees lines that could clear the size gate.

    Three kinds of line always go through: H-shares (HKEX counts the H
    tranche only - PetroChina's H-shares are an eighth of the company), lines
    with no HKEX figure (nothing to judge by), and anything at or above
    `pregate_fraction` of the floor. The gate proper is Yahoo's market cap,
    applied later; this only decides what is worth asking Yahoo about.
    """
    cap_usd = pd.to_numeric(df.get("hkex_mcap_hkd"), errors="coerce") * usd_per_hkd
    h = df.get("is_h_share", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    keep = h | cap_usd.isna() | (cap_usd >= cfg.min_market_cap_usd * cfg.pregate_fraction)
    stats = {"pregate_sent_to_yahoo": int(keep.sum()),
             "pregate_h_shares": int((keep & h).sum()),
             "pregate_no_hkex_cap": int((keep & cap_usd.isna() & ~h).sum())}
    return df[keep].copy(), stats


def repair_market_caps(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """HKEX's market cap where Yahoo has none - never for an H-share.

    Germany's invariant 11 (a missing market cap is repaired, not read as
    small), with the exchange's own figure as the source, as Poland used
    GPW's. For a company whose shares all list in Hong Kong it is the whole
    company; for an H-share it is one tranche, so an H-share without a Yahoo
    figure stays missing rather than being gated on a fraction of itself.
    """
    df = df.copy()
    mc = pd.to_numeric(df.get("market_cap_local"), errors="coerce")
    hk = pd.to_numeric(df.get("hkex_mcap_hkd"), errors="coerce")
    h = df.get("is_h_share", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    fill = mc.isna() & hk.notna() & ~h
    df.loc[fill, "market_cap_local"] = hk[fill]
    df.loc[fill, "mcap_source"] = "hkex"
    return df, {"mcap_via_hkex": int(fill.sum())}


def attach_classification(df: pd.DataFrame) -> pd.DataFrame:
    """Yahoo's sector and industry as the peer keys, under the names every
    other market uses (`industry` the peer group, `sector` its fallback), so
    screener.py and the dashboard read them unchanged."""
    df = df.copy()
    df["industry"] = df.get("yf_industry", pd.Series("", index=df.index)).fillna("").astype(str)
    df["sector"] = df.get("yf_sector", pd.Series("", index=df.index)).fillna("").astype(str)
    return df


def apply_yahoo_filters(df: pd.DataFrame, cfg: K.ScreenConfig,
                        stats: dict) -> tuple[pd.DataFrame, dict]:
    """What needs Yahoo's classification, on the priced lines.

    Property (config_hk.PROPERTY_INDUSTRIES): Hong Kong's landlords revalue
    their buildings through earnings under HKAS 40 and trade at a standing
    discount to an appraised book, and China's developers are priced on
    their credit, not their book - Germany's landlord reasoning.
    REITs that HKEX files as equity, and fund quotes, go with them. Holding
    companies are tagged, never dropped by default.
    """
    df = df.copy()
    name = df["name"].fillna("").astype(str)
    ind = df.get("industry", pd.Series("", index=df.index)).fillna("").astype(str)
    qtype = df.get("quote_type", pd.Series("", index=df.index)).fillna("").astype(str)
    df["is_reit_yahoo"] = pd.Series([K.is_reit(n, i) for n, i in zip(name, ind)],
                                    index=df.index)
    df["is_property"] = ind.map(K.is_property) & ~df["is_reit_yahoo"]
    df["is_fund_quote"] = qtype.str.upper().isin(["ETF", "MUTUALFUND", "FUND"])
    df["is_holdco"] = df["code"].map(K.is_holdco)
    df["holdco_note"] = df["code"].map(K.KNOWN_HOLDCOS).fillna("")

    stats = dict(stats)
    for flag, key, on in (("is_reit_yahoo", "dropped_reit_yahoo", cfg.exclude_reits),
                          ("is_property", "dropped_property", cfg.exclude_property),
                          ("is_fund_quote", "dropped_fund_quote", True),
                          ("is_holdco", "dropped_holdco", cfg.exclude_holdcos)):
        if on:
            stats[key] = int(df[flag].sum())
            df = df[~df[flag]]
    if not cfg.exclude_holdcos:
        stats["flagged_holdco"] = int(df["is_holdco"].sum())
    stats["industry_missing"] = int(df["industry"].eq("").sum())
    return df.copy(), stats


def keep_one_line_per_issuer(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Two share classes of one company: keep the traded line.

    Germany's invariant 1. Swire Pacific lists A shares (00019) and B shares
    (00087) - the B carries a fifth of an A's economic interest and the same
    vote - so both lines price one set of earnings, and the cheaper would be
    scored against a cohort holding its sibling. The most traded line
    survives; ties go to the line whose name carries no class letter.
    """
    df = df.copy()
    df["issuer"] = df["name"].map(K.issuer_key)
    df["n_lines"] = df.groupby("issuer")["issuer"].transform("size")
    adv = pd.to_numeric(df.get("adv_local"), errors="coerce").fillna(-1.0)
    df["_a"] = adv
    df["_o"] = (~df["name"].fillna("").str.upper().str.strip().str.endswith(" B")).astype(int)
    df = df.sort_values(["issuer", "_a", "_o"], ascending=[True, False, False])
    dup = df.duplicated(subset=["issuer"], keep="first")
    dropped = df.loc[dup, "ticker"].tolist()
    if dropped:
        log.info("one line per issuer: dropped %s", ", ".join(dropped))
    df = df[~dup].drop(columns=["_a", "_o"])
    return df, {"dropped_second_line": len(dropped)}


# ---------------------------------------------------------------------------
# Control and pyramids
# ---------------------------------------------------------------------------
def apply_control(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Closely held: insiders and strategic holders own a majority.

    France's flag. Yahoo's heldPercentInsiders lumps a family, a parent and a
    state shareholder together, so this says the free float is the minority,
    not who controls. For an H-share it counts the H tranche only, so the
    state's A-share stake does not show - the `h` tag says that instead.
    """
    df = df.copy()
    ins = pd.to_numeric(df.get("insiders_pct"), errors="coerce")
    df["ctrl_pct"] = ins * 100.0
    df["is_controlled"] = ins >= K.CONTROL_MIN_INSIDERS
    stats = {"insiders_known": int(ins.notna().sum()),
             "flagged_majority_insiders": int(df["is_controlled"].sum())}
    if cfg.exclude_controlled:
        stats["dropped_controlled"] = int(df["is_controlled"].sum())
        df = df[~df["is_controlled"]]
    return df, stats


def _latest(row: pd.Series, key: str) -> float:
    for i in (3, 2, 1):
        v = pd.to_numeric(row.get(f"{key}_y{i}"), errors="coerce")
        if pd.notna(v):
            return float(v)
    return np.nan


def flag_pyramids(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """A listed parent that consolidates a listed subsidiary.

    France's test, unchanged: Christian Dior over LVMH there; in Hong Kong
    the shape is Hang Lung Group over Hang Lung Properties. Both in one
    screen counts one set of earnings twice, and the parent - which carries
    a standing holding discount - is scored against a cohort that holds its
    own subsidiary. A pair needs all three of
      * revenue within PYRAMID_REVENUE_TOL and operating profit within
        PYRAMID_OP_TOL, same currency and fiscal year - one income statement
      * a parent whose equity is at least PYRAMID_MIN_MI_SHARE minority
        interests (the subsidiary's other shareholders)
      * a child carrying far fewer (PYRAMID_MI_GAP below the parent)
      * the same Yahoo sector - one income statement is one business
    Revenue alone is not enough: France's first run matched ten unrelated
    pairs on it. All of France's three were not enough here: Hong Kong's
    first run paired Yankuang Energy (coal, heavy minorities) with Muyuan
    Foods (hogs) - revenue 144.9bn against 144.1bn, operating profit within
    5% - and dropped a coal miner as a pig farmer's parent. The parent is
    dropped by default and the subsidiary tagged.
    """
    df = df.copy()
    df["pyramid_parent_of"] = ""
    df["pyramid_child_of"] = ""
    rev = df.apply(lambda r: _latest(r, "rev"), axis=1)
    op = df.apply(lambda r: _latest(r, "op"), axis=1)
    fy = df.get("fin_years", pd.Series("", index=df.index)).fillna("").astype(str) \
           .str.split(",").str[-1]
    ccy = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str)
    eq = pd.to_numeric(df.get("lf_equity"), errors="coerce")
    mi = pd.to_numeric(df.get("lf_mi"), errors="coerce").fillna(0.0)
    mi_share = mi / (eq + mi)
    sec = df.get("sector", pd.Series("", index=df.index)).fillna("").astype(str)
    idx = [i for i in df.index if pd.notna(rev[i]) and rev[i] > 0]
    pairs = []
    for a_pos, a in enumerate(idx):
        for b in idx[a_pos + 1:]:
            if fy[a] != fy[b] or ccy[a] != ccy[b] or not fy[a] or sec[a] != sec[b]:
                continue
            if abs(rev[a] - rev[b]) / max(rev[a], rev[b]) > K.PYRAMID_REVENUE_TOL:
                continue
            if not (pd.notna(op[a]) and pd.notna(op[b]) and op[a] * op[b] > 0
                    and abs(op[a] - op[b]) / max(abs(op[a]), abs(op[b])) <= K.PYRAMID_OP_TOL):
                continue
            parent, child = (a, b) if mi_share[a] >= mi_share[b] else (b, a)
            if (mi_share[parent] >= K.PYRAMID_MIN_MI_SHARE
                    and mi_share[parent] - mi_share[child] >= K.PYRAMID_MI_GAP):
                pairs.append((parent, child))
    for p, c in pairs:
        df.at[p, "pyramid_parent_of"] = str(df.at[c, "name"])
        df.at[c, "pyramid_child_of"] = str(df.at[p, "name"])
        df.at[p, "is_holdco"] = True
        log.info("pyramid: %s consolidates %s (revenue %.0f vs %.0f)",
                 df.at[p, "name"], df.at[c, "name"], rev[p], rev[c])
    parents = df["pyramid_parent_of"].ne("")
    stats = {"pyramid_pairs": len(pairs)}
    if cfg.exclude_pyramid_parents:
        stats["dropped_pyramid_parent"] = int(parents.sum())
        df = df[~parents]
    return df, stats


# ---------------------------------------------------------------------------
# The currency repair
# ---------------------------------------------------------------------------
# A reporting currency whose rate to the Hong Kong dollar is within this
# factor of 1 cannot be told apart from it by magnitude at all (France's
# rule; the pataca, ~0.97, is the local case). Refused.
FX_MIN_SEPARATION = 1.08
# A ratio within this factor of a hypothesis supports it. Measured
# 2026-10-14 on 536 renminbi reporters past the size gate: EPS x shares / net
# income over the trailing-year average rate has p5-p95 of 0.83-1.21 and an
# interquartile range of 0.96-1.01; the tails are EPS rounded to two places,
# loss years and share counts that moved. 1.25 keeps the body and refuses
# the debris.
FX_BAND = 1.25
# The reporting-currency reading is only ever accepted where the two
# hypotheses are this far apart - the dollar (7.85), euro (8.8), Singapore
# dollar (6.1), yen (0.049). Not the renminbi: at 1.14-1.17 its two
# hypotheses sit inside one another's band, and on Hong Kong lines Yahoo has
# never been seen on the reporting basis - 0 of 67 dollar reporters for EPS,
# 0 of 12 for book value. So where the test cannot separate them, Yahoo's
# measured convention is assumed, and the arithmetic of that choice is the
# safe one: were a renminbi EPS or book value ever read as Hong Kong
# dollars, the multiple would come out ~15% DEARER, never cheaper.
FX_DECISIVE = 1.5


def _basis(k: pd.Series, r: pd.Series, decisive: pd.Series) -> pd.Series:
    """'quote' where k is within FX_BAND of r, 'reporting' where it is within
    FX_BAND of 1 and the currencies are decisively apart, '' otherwise
    (untestable, or evidence of neither)."""
    lk = np.log(k.where(k > 0))
    lr = np.log(r.where(r > 0))
    band = np.log(FX_BAND)
    quote = (lk - lr).abs() < band
    rep = decisive & (lk.abs() < band) & ~quote
    return pd.Series(np.where(quote, "quote", np.where(rep, "reporting", "")), index=k.index)


def repair_foreign_reporters(df: pd.DataFrame, hkd_per: dict, hkd_per_ttm: dict | None = None,
                             q_equity: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """Yahoo's multiples on Hong Kong dollar lines of companies that report
    in another currency - most of the market by value.

    France's repair, measured again on HKEX (2026-10-14). Yahoo's market cap,
    EV, EPS and book value per share on these lines are in Hong Kong dollars;
    its EBITDA and net income are in the reporting currency. So P/E and P/B
    are right as Yahoo states them and EV/EBITDA is not - AIA's reads 86.5, a
    Hong Kong dollar EV over a US dollar EBITDA. Each field is still tested
    per row, never assumed, and against the rate Yahoo actually used:

      EPS  k = EPS x shares / net income         vs the TRAILING-YEAR AVERAGE
           rate - the trailing EPS sums four quarters converted at four
           rates. Against spot the renminbi ratio sat at 0.95 with a quarter
           of names near the midpoint; against the average, at 0.98.
      BPS  k = BPS x shares / latest interim equity   vs SPOT - measured
           k / spot = 1.000 wherever the interim is Yahoo's latest period
           (no interim: the filed annual, then Yahoo's own ROE)
      EV   EV / market cap in [0.25, 4], France's band, OR within FX_BAND
           of today's market cap plus filed net debt and minorities in Hong
           Kong dollars: a Hong Kong dollar EV, so EV/EBITDA = EV / (EBITDA x
           the average rate), EBITDA being a trailing flow like earnings.
           The second route is Hong Kong's: an indebted state company at a
           fifth of book carries an EV of 5-12x its market cap (China
           Railway 8.2, CNBM 11.9, Huaneng 4.8), which France's band refused
           although Yahoo's figure matched the filed debt to within 12%

    With r the rate, k near r says Hong Kong dollars (kept), k near 1 the
    reporting currency (repaired - only where FX_DECISIVE allows), anything
    else refuses the multiple it vouches for. `shares` is Yahoo's implied
    count - all shares, A and H, the count its market cap is built on.
    """
    df = df.copy()
    q_equity = q_equity or {}
    hkd_per_ttm = hkd_per_ttm or hkd_per

    def n(c: str) -> pd.Series:
        if c not in df.columns:
            return pd.Series(np.nan, index=df.index)
        return pd.to_numeric(df[c], errors="coerce")
    fin = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str)
    quote = df.get("currency", pd.Series("HKD", index=df.index)).fillna("HKD").astype(str)
    foreign = fin.ne("") & fin.ne(quote) & quote.eq("HKD")
    r = fin.map(lambda c: hkd_per.get(c, np.nan)).astype(float)
    rt = fin.map(lambda c: hkd_per_ttm.get(c, np.nan)).astype(float).fillna(r)
    df["fx_rep_to_hkd"] = r.where(foreign, 1.0)
    df["fx_note"] = ""
    stats = {"foreign_reporters": int(foreign.sum())}
    if not foreign.any():
        return df, stats

    untestable = foreign & ~(np.abs(np.log(r)) > np.log(FX_MIN_SEPARATION))
    live = foreign & ~untestable
    decisive = np.abs(np.log(r)) >= np.log(FX_DECISIVE)

    eps, bps, sh = n("trailing_eps"), n("book_value_ps"), n("yf_shares")
    ni, roe = n("yf_ni"), n("yf_roe")
    ev, mcap, ebitda = n("yf_ev"), n("market_cap_local"), n("yf_ebitda")

    eps_basis = _basis((eps * sh / ni).where(ni.abs() > 0), rt, decisive)
    eps_rep = live & eps_basis.eq("reporting")
    # Only a positive figure can be vouched for or refused; a loss is
    # already a missing multiple (invariant 2).
    eps_bad = live & eps_basis.eq("") & (eps > 0)
    eps_hkd = eps.where(~eps_rep, eps * rt)

    qeq = df["ticker"].map(q_equity).astype(float)
    by_quarter = _basis((bps * sh / qeq).where((qeq > 0) & (sh > 0)), r, decisive)
    eq = n("lf_equity")
    lsh = n("lf_shares") * n("share_basis_g").fillna(1.0)
    by_filing = _basis((bps * lsh / eq).where((eq > 0) & (lsh > 0)), r, decisive)
    # Yahoo's ROE is unit-free; EPS over BPS matches it when both are in Hong
    # Kong dollars and runs r times too high when BPS is in the reporting
    # currency. So r over that ratio lands near r (HKD) or near 1 (reporting).
    k_roe = ((eps_hkd / bps) / roe).where((roe > 0) & (bps > 0) & (eps_hkd > 0))
    by_roe = _basis(r / k_roe, r, decisive)
    has_q = (qeq > 0) & (sh > 0) & bps.notna()
    has_filing = (eq > 0) & (lsh > 0) & bps.notna()
    bps_basis = by_quarter.where(has_q, by_filing.where(has_filing, by_roe))
    bps_rep = live & bps_basis.eq("reporting")
    bps_bad = live & bps_basis.eq("") & (bps > 0)

    own_ev = mcap + (n("lf_debt") - n("lf_cash") + n("lf_mi").fillna(0.0)) * r
    ev_hkd = live & ((ev / mcap).between(0.25, 4.0)
                     | (np.log((ev / own_ev).where((ev > 0) & (own_ev > 0))).abs()
                        < np.log(FX_BAND)))
    # Financials have no EV/EBITDA to repair (invariant 6), and a loss-making
    # EBITDA none to vouch for; neither earns an "EV/EBITDA refused" note.
    ev_moot = _fin_mask(df) | ~(ebitda > 0)

    df["trailing_eps"] = eps_hkd.where(~eps_bad)
    df["trailing_pe"] = n("trailing_pe").where(~eps_rep, n("trailing_pe") / rt).where(~eps_bad)
    df["book_value_ps"] = bps.where(~bps_rep, bps * r).where(~bps_bad)
    df["price_to_book"] = n("price_to_book").where(~bps_rep, n("price_to_book") / r) \
                                            .where(~bps_bad)
    evx = (ev / (ebitda * rt)).where(ebitda > 0)
    df["ev_to_ebitda"] = n("ev_to_ebitda").where(~foreign, evx.where(ev_hkd))
    for c in ("trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps", "book_value_ps"):
        df.loc[untestable, c] = np.nan

    notes = []
    for i in df.index[foreign]:
        if untestable[i]:
            notes.append((i, "fx basis untestable"))
            continue
        bits = []
        if eps_rep[i]:
            bits.append("P/E repaired")
        elif eps_bad[i]:
            bits.append("P/E refused")
        if bps_rep[i]:
            bits.append("P/B repaired")
        elif bps_bad[i]:
            bits.append("P/B refused")
        if not ev_moot[i]:
            bits.append("EV/EBITDA repaired" if ev_hkd[i] and pd.notna(evx[i])
                        else "EV/EBITDA refused")
        if bits:
            notes.append((i, ", ".join(bits)))
    for i, t in notes:
        df.at[i, "fx_note"] = t

    stats.update({
        "fx_untestable": int(untestable.sum()),
        "fx_eps_on_reporting_basis": int(eps_rep.sum()),
        "fx_pe_refused": int(eps_bad.sum()),
        "fx_pb_repaired": int(bps_rep.sum()),
        "fx_pb_refused": int(bps_bad.sum()),
        "fx_ev_repaired": int((ev_hkd & evx.notna()).sum()),
    })
    log.info("currency repair: %d foreign reporters (%s)", int(foreign.sum()),
             ", ".join(f"{k} {v}" for k, v in stats.items() if k != "foreign_reporters"))
    return df, stats


# ---------------------------------------------------------------------------
# Cash flow: P/FCF and the shareholder yield
# ---------------------------------------------------------------------------
def add_cashflow_metrics(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """P/FCF (a fourth peer metric) and the shareholder yield, from the latest
    filed cash-flow statement (providers_hk.cashflow_fields).

    Both are struck on today's market value in the REPORTING currency over
    the filed year - the basis the own-history screen already uses - so a
    foreign reporter needs no extra repair. P/FCF is missing for financials,
    as EV/EBITDA is (invariant 6): a bank's operating cash flow moves with
    its deposits and loans, not its earnings. The shareholder yield is
    dividends plus buybacks less shares issued, so a company that buys back
    stock only to offset employee grants does not get credit for it.
    """
    df = df.copy()
    n = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731
    fx = n("fx_now")
    same = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str) \
             .eq(df.get("currency", pd.Series("", index=df.index)).fillna("").astype(str))
    fx = fx.where(fx.notna(), np.where(same, 1.0, np.nan))
    mv = n("market_cap_local") * fx
    fcf = n("lf_fcf")
    pfcf = (mv / fcf).where((fcf > 0) & (mv > 0))
    lo, hi = K.METRIC_BOUNDS["price_to_fcf"]
    df["price_to_fcf"] = pfcf.where((pfcf >= lo) & (pfcf <= hi)).where(~_fin_mask(df))
    paid = n("lf_div_paid") + n("lf_buyback") - n("lf_issuance")
    df["sh_yield"] = (paid / mv * 100.0).where(mv > 0)
    df["fcf_negative"] = fcf <= 0
    stats = {"cashflow_known": int(fcf.notna().sum()),
             "pfcf_valid": int(df["price_to_fcf"].notna().sum()),
             "sh_yield_known": int(df["sh_yield"].notna().sum())}
    return df, stats


# ---------------------------------------------------------------------------
# One-off earnings
# ---------------------------------------------------------------------------
def _hist_oneoff_ratio(df: pd.DataFrame, tax: float) -> pd.Series:
    """Median of net profit over after-tax operating profit in the filed
    years BEFORE the latest one (np_y1/op_y1, np_y2/op_y2). High means the
    gap is the company's structure (a trading house's equity-method income),
    not a one-off."""
    rs = []
    for i in (1, 2):
        np_ = pd.to_numeric(df.get(f"np_y{i}"), errors="coerce")
        op = pd.to_numeric(df.get(f"op_y{i}"), errors="coerce")
        rs.append((np_ / (op * (1 - tax))).where(op > 0))
    return pd.concat(rs, axis=1).median(axis=1, skipna=True)


def apply_oneoff_guard(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Earnings that came from below the operating line, capped.

    Yahoo's trailing P/E and the ROE built from it take net income as
    reported, one-offs included: Lyft's P/E of 2.5 and ROE of 85% are a tax
    valuation-allowance release (net income USD 2.87bn on an operating loss
    over four quarters), Boyd's a gain on selling its FanDuel stake, FIS's
    the Worldpay sale. Germany's K+S and Hong Kong's 3SBio were the same
    shape. The test: net income above ONEOFF_RATIO times operating profit
    after a statutory tax rate, both summed over the last four QUARTERS from
    one statement (providers.ttm_fields) - or the latest filed year where
    four quarters are not available (half-yearly reporters). Ordinary
    companies sit at 1.0-1.2 (Microsoft 1.15, Qualcomm 1.20, Deckers 1.08).
    Not .info's operatingMargins: that is one quarter's margin, and it made
    141 flags out of 2,285 on the first run, most of them false.

    It fires only where the gap is NEW: if the earlier filed years show the
    same ratio, the earnings are the company's structure - Mitsubishi
    Corporation's equity-method income reads 2.1 every year - and are left
    alone. Financials are exempt; a bank's "operating income" is not the
    line this test needs.

    A flagged row is scored on capped earnings - operating profit after tax
    - so its P/E and EPS (and with it the ROE) are restated before the peer
    medians are built. An operating loss under a reported profit leaves no
    P/E at all. The row is tagged; nothing is dropped.
    """
    df = df.copy()
    tax = cfg.oneoff_tax_pct / 100.0
    n = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731
    quarters = n("ttm_op").notna() & n("ttm_ni").notna()
    ni = n("ttm_ni").where(quarters, n("lf_np_rep"))
    op = n("ttm_op").where(quarters, n("lf_op"))
    df["oneoff_basis"] = np.where(quarters, "4 quarters",
                                  np.where(op.notna() & ni.notna(), "filed year", ""))
    cap = op * (1 - tax)
    ratio = (ni / cap).where(cap > 0)
    ratio = ratio.where(~((op <= 0) & (ni > 0)), np.inf)
    hist = _hist_oneoff_ratio(df, tax)
    structural = hist > cfg.oneoff_ratio
    flag = (~_fin_mask(df) & (ni > 0) & (ratio > cfg.oneoff_ratio) & ~structural
            & cfg.oneoff_guard)
    k = (cap.clip(lower=0) / ni).where(flag)
    eps, pe = n("trailing_eps"), n("trailing_pe")
    df["trailing_eps"] = eps.where(~flag, eps * k)
    df["trailing_pe"] = pe.where(~flag, (pe / k).where(k > 0))
    df["oneoff_ratio"] = ratio.replace(np.inf, np.nan)
    df["oneoff_capped"] = flag
    df["oneoff_structural"] = structural & (ratio > cfg.oneoff_ratio) & ~_fin_mask(df)
    stats = {"oneoff_capped": int(flag.sum()),
             "oneoff_on_quarters": int(quarters.sum()),
             "oneoff_no_operating_profit": int((flag & ~(cap > 0)).sum()),
             "oneoff_structural_spared": int(df["oneoff_structural"].sum())}
    if flag.any():
        log.info("one-off guard: %d trailing P/Es restated on operating profit (%s)",
                 int(flag.sum()), ", ".join(df.loc[flag, "ticker"].astype(str).head(25)))
    return df, stats


def add_valueup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Low-P/B flags.

    Korea had a live policy catalyst attached to almost exactly this screen -
    KRX publicly identifies firms whose PBR sits in the bottom 20% of their
    industry. Hong Kong has no counterpart: no disclosure regime is keyed to
    valuation. The nearest thing is on the mainland - the CSRC's 2024
    market-value-management guideline asks A-share companies that trade
    below book for a year to publish a valuation-improvement plan - which
    reaches Hong Kong only through companies that also list A-shares, and
    is not computable from this data. What closes a Hong Kong discount is usually a privatisation offer
    by the controlling holder.

    So these two columns are kept for continuity and for sorting, not as a
    catalyst.
    """
    df = df.copy()
    rank = df.get("price_to_book_pct_rank")
    df["pbr_bottom20_industry"] = (rank <= 0.20) if rank is not None else False
    df["pbr_below_1"] = df["price_to_book"] < 1.0
    return df


def add_quality_context(df: pd.DataFrame) -> pd.DataFrame:
    """ROE derived from EPS and BPS rather than taken as a vendor field.

    Same reasoning as Korea: the ratio is then consistent with the very P/E
    and P/B being screened on. It is unit-free only once EPS and BPS are in
    the same currency - Yahoo hands out reporting-currency figures beside Hong
    Kong dollar ones for foreign reporters, so this must run after
    repair_foreign_reporters.
    """
    df = df.copy()
    eps = pd.to_numeric(df.get("trailing_eps"), errors="coerce")
    bps = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    df["roe_pct"] = np.where((bps > 0) & eps.notna(), eps / bps * 100.0, np.nan)
    df["div_yield"] = pd.to_numeric(df.get("div_yield"), errors="coerce")
    df["pays_dividend"] = df["div_yield"].fillna(0) > 0
    return df


def flag_ttm_vs_filed(df: pd.DataFrame) -> pd.DataFrame:
    """Profitable over the trailing twelve months, loss-making in the last
    filed year. A flag, never a gate.

    The peer and absolute screens use Yahoo's trailing multiples in every
    market, and Yahoo's trailing twelve months is the sum of the last four
    quarters - one-offs included. Germany's K+S is the case that found this:
    two quarters of very large non-operating gains turned a filed loss of
    EUR 1.1bn into TTM net income of +EUR 1.06bn and a P/E of 2.6.

    It is not a sign error - the quarters genuinely sum that way - so the
    trailing figure is left alone and the row is marked instead. The test is
    deliberately narrow: TTM profit against a normalized filed LOSS. A ratio
    test (TTM P/E far below filed P/E) would also catch genuine turnarounds,
    which is noise, not a warning.
    """
    df = df.copy()
    pe = pd.to_numeric(df.get("trailing_pe"), errors="coerce")
    lf = pd.to_numeric(df.get("lf_ni"), errors="coerce")
    df["ttm_vs_filed_loss"] = pe.notna() & (lf < 0)
    return df


def apply_roe_gate(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Require survivors to actually earn something.

    Runs after scoring, never before: it narrows `passes` and leaves
    avg_discount alone, so the peer cohorts still contain the low-ROE names
    that make them representative. Missing ROE fails.
    """
    df = df.copy()
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    df["roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)
    df["roe_tier"] = np.select(
        [roe >= 15.0, roe >= cfg.roe_good_pct, roe >= cfg.min_roe_pct],
        ["strong", "good", "marginal"], default="fail")

    before = int(df["passes"].sum())
    df["passes"] = df["passes"] & df["roe_ok"]
    after = int(df["passes"].sum())
    stats = {f"dropped_roe_below_{cfg.min_roe_pct:g}": before - after,
             "passing_after_roe": after}
    df = df.sort_values(["passes", "avg_discount"], ascending=[False, False])
    return df, stats


def apply_absolute_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Absolute cheapness, independent of the peer comparison.

    Identical in structure to the Korea version, including the financials
    carve-out: EV/EBITDA is suppressed for banks and insurers, so a strict
    both-metrics rule would exclude every Hong Kong bank and insurer however far
    below book it traded.
    """
    df = df.copy()
    pbr = pd.to_numeric(df.get("price_to_book"), errors="coerce")
    ev = pd.to_numeric(df.get("ev_to_ebitda"), errors="coerce")
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    # The payout test is the SHAREHOLDER yield - dividends plus net buybacks
    # over market value (add_cashflow_metrics) - with the dividend yield where
    # no cash-flow statement was filed. The user's 2% floor stays.
    shy = pd.to_numeric(df.get("sh_yield", pd.Series(np.nan, index=df.index)),
                        errors="coerce")
    dy = shy.where(shy.notna(), pd.to_numeric(df.get("div_yield"), errors="coerce"))
    df["payout_yield"] = dy
    fin = _fin_mask(df)

    df["abs_pbr_ok"] = pbr.notna() & (pbr < cfg.abs_max_pbr)
    df["abs_ev_ok"] = ev.notna() & (ev < cfg.abs_max_ev_ebitda)
    df["abs_ev_missing"] = ev.isna()
    df["abs_roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)

    fair_pbr = roe / cfg.abs_cost_of_equity_pct
    df["abs_fair_pbr"] = fair_pbr
    df["abs_pbr_vs_roe_ok"] = pbr.notna() & fair_pbr.notna() & (pbr < fair_pbr)
    df["abs_div_ok"] = dy.notna() & (dy >= cfg.abs_min_div_yield)

    core = df["abs_pbr_ok"] & df["abs_ev_ok"]
    if cfg.abs_financials_pbr_only:
        core = core | (fin & df["abs_pbr_ok"])
        df["abs_via_carveout"] = fin & df["abs_pbr_ok"] & ~df["abs_ev_ok"]
    else:
        df["abs_via_carveout"] = False

    if cfg.abs_require_roe:
        core = core & df["abs_roe_ok"]
    if cfg.abs_require_pbr_vs_roe:
        core = core & df["abs_pbr_vs_roe_ok"]
    if cfg.abs_min_div_yield > 0:
        core = core & df["abs_div_ok"]
    df["abs_passes"] = core

    rel = df["passes"].astype(bool)
    absp = df["abs_passes"].astype(bool)
    df["screen"] = np.select([rel & absp, rel & ~absp, ~rel & absp],
                             ["both", "relative", "absolute"], default="")
    df["passes_any"] = rel | absp

    stats = {
        f"abs_pbr_under_{cfg.abs_max_pbr:g}": int(df["abs_pbr_ok"].sum()),
        f"abs_ev_under_{cfg.abs_max_ev_ebitda:g}": int(df["abs_ev_ok"].sum()),
        "abs_pbr_below_fair": int(df["abs_pbr_vs_roe_ok"].sum()),
        f"abs_payout_over_{cfg.abs_min_div_yield:g}pct": int(df["abs_div_ok"].sum()),
        "abs_payout_from_dividends_only": int((df["abs_div_ok"] & shy.isna()).sum()),
        "abs_passing": int(absp.sum()),
        "abs_via_financial_carveout": int((absp & df["abs_via_carveout"]).sum()),
        "abs_new_vs_relative": int((absp & ~rel).sum()),
        "passing_either_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def restate_info_for_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Yahoo's .info per-share fields on today's share basis, after a split.

    The peer and absolute screens read P/E, P/B, EPS, book value and yield
    straight from .info. Yahoo restates those for splits on its own schedule,
    and not consistently: after Johnson Matthey's Aug 2026 4-for-3
    consolidation its bookValue was already on today's 125.9m shares (16.01 =
    FY2026 equity / 125.9m) while its filed statements were still on the old
    167.9m; in the Japan build bookValue was restated while dividendRate was
    not. So the basis is tested per row, never assumed either way.

    `share_basis_g` comes from build_statement_record: the split / consolidation
    factor since the latest filing that today's market cap demands (1 for
    almost everyone). Where it is not 1, filed equity over (book value x filed
    shares) says which basis .info is on: ~1 means the old one, ~g the new.

      old basis   EPS, book value and yield divided by g; P/E and P/B
                  multiplied by it. ROE, their ratio, does not move.
      new basis   per-share fields are right; the dividend cannot be checked
                  the same way (in Japan it lagged when book value did not),
                  so the yield is left missing rather than trusted.
      neither     P/E, P/B and yield missing - no basis to vouch for.

    EV/EBITDA needs nothing: Yahoo builds EV from today's market cap.
    """
    df = df.copy()
    g = pd.to_numeric(df.get("share_basis_g"), errors="coerce").fillna(1.0)
    eq = pd.to_numeric(df.get("lf_equity"), errors="coerce")
    sh = pd.to_numeric(df.get("lf_shares"), errors="coerce")
    bv = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    k = eq / (bv * sh)
    moved = np.abs(np.log(g)) > 0.02
    tol = np.log(1.25)
    old = moved & (np.abs(np.log(k)) < tol)
    new = moved & ~old & (np.abs(np.log(k / g)) < tol)
    unclear = moved & ~old & ~new

    for c, op in (("trailing_eps", "div"), ("book_value_ps", "div"), ("div_yield", "div"),
                  ("trailing_pe", "mul"), ("price_to_book", "mul")):
        if c in df.columns:
            v = pd.to_numeric(df[c], errors="coerce")
            df[c] = v.where(~old, v / g if op == "div" else v * g)
    if "div_yield" in df.columns:
        df.loc[new, "div_yield"] = np.nan
    for c in ("trailing_pe", "price_to_book", "div_yield"):
        if c in df.columns:
            df.loc[unclear, c] = np.nan

    df["split_note"] = np.select([old, new, unclear],
                                 ["info restated for split", "yield unverified after split",
                                  "info basis unclear"], default="")
    stats = {"info_restated_for_split": int(old.sum()),
             "info_yield_unverified": int(new.sum()),
             "info_basis_unclear": int(unclear.sum())}
    if moved.any():
        log.info("split basis: %d .info rows restated, %d yields unverified, %d unclear",
                 *stats.values())
    return df, stats


HIST_METRICS = (("per", "trailing_pe"), ("pbr", "price_to_book"),
                ("evx", "ev_to_ebitda"))


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple, np.ndarray)) else []


def add_history_now(df: pd.DataFrame) -> pd.DataFrame:
    """Today's P/E, P/B and EV/EBITDA on the SAME basis as the history.

    Each historical year is that year-end market value over that year's filed
    totals (providers_hk.build_statement_record). Today's value has to be
    built the same way - today's market value over the latest filing - or the
    comparison measures the gap between two definitions rather than a change
    in valuation.

    That is not hypothetical. yfinance's own trailingPE divides by the last
    twelve months, including interims: the UK build measured Shell at 10.5
    against 15.2 on the filed-year basis, a 31% gap that would read as a 31%
    discount to history on its own. Korea learned the same lesson on
    EV/EBITDA, where mixing providers put only 18 of 30 within +/-25%; its fix
    was to rebuild the current value from the history's own source, which is
    what this does.

    So these three columns feed the own-history screen ONLY. The peer and
    absolute screens keep yfinance's figures, because they compare companies
    with each other on a common vendor definition, not a company with itself.

    Out-of-bounds values become NaN here rather than in the screen, so the
    dashboard receives exactly what Python tested and cannot disagree with it.
    """
    df = df.copy()
    n = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731
    mv = n("market_cap_local") * n("fx_now")      # quote-major -> reporting ccy
    ni, eq, eb = n("lf_ni"), n("lf_equity"), n("lf_ebitda")
    mi = n("lf_mi").fillna(0.0)
    df["per_now"] = (mv / ni).where(ni > 0)
    df["pbr_now"] = (mv / eq).where(eq > 0)
    df["evx_now"] = ((mv + n("lf_debt") - n("lf_cash") + mi) / eb).where(eb > 0)
    df.loc[_fin_mask(df), "evx_now"] = np.nan          # invariant 6
    for col, (_, bound_key) in zip(("per_now", "pbr_now", "evx_now"), HIST_METRICS):
        lo, hi = K.METRIC_BOUNDS[bound_key]
        df[col] = df[col].where((df[col] >= lo) & (df[col] <= hi))
    return df


def apply_history_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Cheap against the company's own filed history - the third screen.

    A port of the Korea build's apply_history_screen, held to the same rules:
    the benchmark is a median (invariant 4); a non-positive or out-of-bounds
    multiple is missing, never cheap, in history as well as today
    (invariant 2), so a loss year drops out of the benchmark rather than
    dragging it; EV/EBITDA is skipped for financials (invariant 6); fewer than
    `hist_min_years` usable years is no benchmark; the ROE floor applies.

    One difference, and it is a data limit rather than a choice: Yahoo carries
    FOUR filed years for Hong Kong companies, as for UK and German ones, where
    WiseReport gave Korea five, so the benchmark is a four-year median.
    """
    df = add_history_now(df)
    fin = _fin_mask(df)

    disc_cols = []
    for key, bound_key in HIST_METRICS:
        lo, hi = K.METRIC_BOUNDS[bound_key]
        vals = df.get(f"hist_{key}", pd.Series([[]] * len(df), index=df.index)) \
                 .map(_as_list)
        for i in range(5):
            df[f"hist_{key}_y{i + 1}"] = vals.map(
                lambda v, i=i: v[i] if i < len(v) and v[i] is not None else np.nan)

        def bench(v):
            ok = [float(x) for x in v if x is not None and np.isfinite(float(x))
                  and lo <= float(x) <= hi]
            return float(np.median(ok)) if len(ok) >= cfg.hist_min_years else np.nan

        med = vals.map(bench)
        if key == "evx":
            med = med.where(~fin)
        cur = df[f"{key}_now"]
        df[f"hist_{key}_med"] = med
        df[f"hist_{key}_disc"] = (med - cur) / med
        disc_cols.append(f"hist_{key}_disc")

    discs = df[disc_cols]
    df["hist_n_valid"] = discs.notna().sum(axis=1)
    df["hist_n_pass"] = (discs >= cfg.hist_min_discount).sum(axis=1)
    # Averages every metric with data, including the failing ones - the same
    # rule as avg_discount (invariant 5).
    df["hist_avg_disc"] = discs.mean(axis=1, skipna=True)

    roe_ok = df.get("roe_ok", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    hp = df["hist_n_pass"] >= cfg.hist_min_metrics
    if cfg.hist_require_roe:
        hp = hp & roe_ok
    df["hist_passes"] = hp

    # Three screens now, so `screen` names every one a row cleared.
    rel = df["passes"].astype(bool)
    absp = df.get("abs_passes", pd.Series(False, index=df.index)).astype(bool)
    parts = pd.DataFrame({"relative": rel, "absolute": absp, "history": hp})
    df["screen"] = parts.apply(lambda r: " + ".join(k for k, v in r.items() if v), axis=1)
    df["passes_any"] = rel | absp | hp

    note = df.get("hist_note", pd.Series("", index=df.index)).fillna("")
    stats = {
        "hist_with_benchmark": int((df["hist_n_valid"] > 0).sum()),
        # Every name a guard refused a benchmark, by reason - so a data problem
        # shows up as a count in the funnel rather than as names quietly
        # missing from the third screen.
        "hist_share_break": int((note == "share-count break").sum()),
        "hist_basis_mismatch": int((note == "price/share basis mismatch").sum()),
        "hist_stale_filing": int((note == "stale filings").sum()),
        "hist_no_reporting_ccy": int((note == "no reporting currency").sum()),
        "hist_normalized_earnings": int((df.get("hist_earnings", pd.Series("", index=df.index))
                                         == "normalized").sum()),
        f"hist_passing_{cfg.hist_min_discount:.0%}": int(hp.sum()),
        "hist_new_vs_other_screens": int((hp & ~rel & ~absp).sum()),
        "passing_any_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def hk_output_columns(cfg: K.ScreenConfig) -> list[str]:
    cols = ["ticker", "symbol", "code", "isin", "name", "name_exchange", "board",
            "sector", "industry", "yf_country", "market_cap_usd", "mcap_source",
            "hkex_mcap_hkd", "adv_usd", "close_local", "currency", "fx_rep_to_hkd", "fx_note",
            "is_h_share", "is_wvr", "is_controlled", "ctrl_pct", "is_holdco", "holdco_note",
            "pyramid_parent_of", "pyramid_child_of"]
    for m in cfg.metrics:
        cols += [m, f"{m}_peer_median", f"{m}_discount",
                 f"{m}_peer_n", f"{m}_pct_rank", f"{m}_peer_basis"]
    cols += ["hist_years", "hist_note", "hist_earnings", "share_basis_g", "split_note",
             "per_now", "pbr_now", "evx_now",
             "hist_n_valid", "hist_n_pass", "hist_avg_disc", "hist_passes"]
    for m in ("per", "pbr", "evx"):
        cols += [f"hist_{m}_med", f"hist_{m}_disc"]
        cols += [f"hist_{m}_y{i}" for i in range(1, 6)]
    cols += ["fin_years", "fin_n", "fin_ccy"]
    for m in ("rev", "op", "ebitda", "np"):
        cols += [f"{m}_y1", f"{m}_y2", f"{m}_y3", f"{m}_cagr"]
    cols += ["screen", "passes_any", "abs_passes", "abs_pbr_ok", "abs_ev_ok",
             "abs_pbr_vs_roe_ok", "abs_div_ok", "abs_roe_ok", "abs_fair_pbr",
             "abs_via_carveout", "ttm_vs_filed_loss",
             "oneoff_capped", "oneoff_ratio", "oneoff_structural",
             "price_to_fcf", "sh_yield", "payout_yield",
             "lf_fcf", "lf_div_paid", "lf_buyback", "lf_issuance",
             "roe_pct", "roe_ok", "roe_tier", "div_yield", "pays_dividend",
             "pbr_below_1", "pbr_bottom20_industry",
             "n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
