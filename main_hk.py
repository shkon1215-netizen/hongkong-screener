"""Hong Kong (HKEX Main Board + GEM) relative-valuation screener.

  python main_hk.py -v                       # full run
  python main_hk.py --skip-liquidity         # size alone gates
  python main_hk.py --include-property       # keep landlords and developers
  python main_hk.py --include-secondary      # keep -S lines (Trip.com, Manulife)
  python main_hk.py --keep-pyramid-parents   # keep a parent next to its listed subsidiary
  python main_hk.py --discount 0.15 --min-metrics 1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import hk_filters as HF
from config_hk import ScreenConfig
from providers_hk import HKEXProvider
from screener import run_screen

D = ScreenConfig()      # argparse defaults come from here, so the two cannot drift


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="hk_screen_results.csv")
    p.add_argument("--min-mcap", type=float, default=D.min_market_cap_usd, help="USD")
    p.add_argument("--min-adv", type=float, default=D.min_adv_usd,
                   help="USD median daily traded value on HKEX")
    p.add_argument("--discount", type=float, default=D.discount_threshold)
    p.add_argument("--min-metrics", type=int, default=D.min_metrics_passing)
    p.add_argument("--min-peers", type=int, default=D.min_peers)
    p.add_argument("--peer-keys", default=",".join(D.peer_keys))
    p.add_argument("--fallback-peer-keys", default=",".join(D.fallback_peer_keys))
    p.add_argument("--fx", type=float, help="USD per HKD (default: live, ~0.128)")
    p.add_argument("--include-investment-cos", action="store_true",
                   help="keep Chapter 21 investment companies (closed-end funds)")
    p.add_argument("--include-reits", action="store_true",
                   help="keep REITs (Link, Champion, Fortune...)")
    p.add_argument("--include-property", action="store_true",
                   help="keep landlords and developers; their P/E carries HKAS 40 "
                        "revaluations and their P/B an appraised book")
    p.add_argument("--include-secondary", action="store_true",
                   help="keep secondary listings (-S), depositary receipts and "
                        "trading-only securities")
    p.add_argument("--keep-pyramid-parents", action="store_true",
                   help="keep a parent that consolidates a listed subsidiary")
    p.add_argument("--exclude-holdcos", action="store_true")
    p.add_argument("--exclude-controlled", action="store_true",
                   help="drop companies whose insiders hold a majority")
    p.add_argument("--no-pregate", action="store_true",
                   help="send every line to Yahoo, not only those whose HKEX market "
                        "cap and measured turnover could clear the gates (~3x slower)")
    p.add_argument("--adv-days", type=int, default=D.adv_lookback_days)
    p.add_argument("--skip-liquidity", action="store_true")
    p.add_argument("--min-roe", type=float, default=D.min_roe_pct,
                   help="ROE%% floor applied to survivors; 0 disables")
    p.add_argument("--abs-pbr", type=float, default=D.abs_max_pbr)
    p.add_argument("--abs-ev", type=float, default=D.abs_max_ev_ebitda)
    p.add_argument("--abs-strict-financials", action="store_true")
    p.add_argument("--no-abs-roe", action="store_true")
    p.add_argument("--no-abs-fair-pbr", action="store_true")
    p.add_argument("--coe", type=float, default=D.abs_cost_of_equity_pct)
    p.add_argument("--no-oneoff-guard", action="store_true",
                   help="score trailing earnings as reported, one-offs included")
    p.add_argument("--abs-min-div", type=float, default=D.abs_min_div_yield)
    p.add_argument("--no-financials", action="store_true",
                   help="skip the 3-year revenue/EBITDA/net-profit history")
    p.add_argument("--no-history", action="store_true",
                   help="skip the own-history screen")
    p.add_argument("--hist-discount", type=float, default=D.hist_min_discount)
    p.add_argument("--hist-min-metrics", type=int, default=D.hist_min_metrics)
    p.add_argument("--dashboard", default="hk_dashboard.html",
                   help="self-contained HTML dashboard; pass '' to skip")
    p.add_argument("--all", action="store_true",
                   help="write every scored row, not just passes")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


# Yahoo's long names end in the legal form ("Tencent Holdings Limited",
# "PetroChina Company Limited"). It says nothing a reader needs.
LEGAL_FORM = re.compile(r"[\s,]+(company limited|co\.?,? ?ltd\.?|limited|ltd\.?|inc\.?|plc"
                        r"|corporation|corp\.?|n\.?v\.?|s\.?a\.?)\s*$", re.IGNORECASE)


def display_name(yahoo: str, hkex: str) -> str:
    """Yahoo's readable name ('Sun Hung Kai Properties') over HKEX's short
    name ('SHK PPT'), legal form removed."""
    n = str(yahoo or "").strip() or str(hkex or "").strip()
    for _ in range(2):
        n = LEGAL_FORM.sub("", n).strip()
    return n


def main() -> int:
    a = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    for noisy in ("urllib3", "peewee", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("hk")

    keys = lambda s: tuple(k.strip() for k in s.split(",") if k.strip())  # noqa: E731
    cfg = ScreenConfig(
        min_market_cap_usd=a.min_mcap,
        min_adv_usd=0.0 if a.skip_liquidity else a.min_adv,
        discount_threshold=a.discount,
        min_metrics_passing=a.min_metrics,
        min_peers=a.min_peers,
        min_roe_pct=a.min_roe,
        abs_max_pbr=a.abs_pbr,
        abs_max_ev_ebitda=a.abs_ev,
        abs_require_roe=not a.no_abs_roe,
        abs_financials_pbr_only=not a.abs_strict_financials,
        abs_require_pbr_vs_roe=not a.no_abs_fair_pbr,
        abs_cost_of_equity_pct=a.coe,
        abs_min_div_yield=a.abs_min_div,
        oneoff_guard=not a.no_oneoff_guard,
        hist_min_discount=a.hist_discount,
        hist_min_metrics=a.hist_min_metrics,
        peer_keys=keys(a.peer_keys),
        fallback_peer_keys=keys(a.fallback_peer_keys),
        exclude_investment_companies=not a.include_investment_cos,
        exclude_reits=not a.include_reits,
        exclude_property=not a.include_property,
        exclude_secondary=not a.include_secondary,
        exclude_pyramid_parents=not a.keep_pyramid_parents,
        exclude_holdcos=a.exclude_holdcos,
        exclude_controlled=a.exclude_controlled,
        adv_lookback_days=a.adv_days,
        pregate_fraction=0.0 if a.no_pregate else D.pregate_fraction,
        adv_pregate=not (a.no_pregate or a.skip_liquidity),
    )

    prov = HKEXProvider(cfg)
    days = prov.recent_business_days(cfg.adv_lookback_days)
    asof = days[-1] if days else datetime.now().strftime("%Y-%m-%d")
    log.info("as of %s", asof)

    # 1. roster: HKEX's List of Securities, one request
    log.info("building roster...")
    roster = prov.listing_roster()
    if roster.empty:
        log.error("empty roster - HKEX's List of Securities did not answer or did not parse")
        return 1
    log.info("roster: %d lines (%s)", len(roster),
             ", ".join(f"{b or 'other'}={n}" for b, n in roster["board"].value_counts().items()))
    if prov.roster_asof:
        age = (pd.Timestamp(asof) - pd.Timestamp(prov.roster_asof)).days
        if age > 10:
            log.warning("HKEX's list is %d days old (as at %s)", age, prov.roster_asof)

    # 2. roster hygiene: every rule here is HKEX's own label, so it runs
    #    before a single Yahoo call.
    df, ustats = HF.apply_roster_filters(roster, cfg)
    log.info("after roster hygiene: %d lines", len(df))

    usd_per_hkd = a.fx if a.fx else prov.hkd_to_usd()
    log.info("FX: 1 HKD = %.5f USD (%.4f HKD per USD)", usd_per_hkd, 1 / usd_per_hkd)

    # 3. the pre-gates (invariant 7: the slow per-ticker work only sees
    #    lines that could clear the gates). First HKEX's own market caps -
    #    a no-op while its feed is dead - then measured turnover, from one
    #    batched price download over every remaining line: a line under the
    #    liquidity floor can never pass the gate that reads the same panel.
    df = df.merge(prov.market_caps(), on="code", how="left")
    df, pstats = HF.pregate(df, usd_per_hkd, cfg)
    ustats.update(pstats)
    log.info("market-cap pre-gate: %d lines left (%d H-shares, %d with no HKEX cap)",
             len(df), pstats["pregate_h_shares"], pstats["pregate_no_hkex_cap"])
    if cfg.adv_pregate:
        log.info("fetching %d days of prices for %d lines (turnover pre-gate)...",
                 cfg.adv_lookback_days, len(df))
        df = df.merge(prov.liquidity(df["ticker"].tolist(), cfg.adv_lookback_days),
                      on="ticker", how="left")
    df, astats = HF.adv_pregate(df, usd_per_hkd, cfg)
    ustats.update(astats)
    log.info("turnover pre-gate: %d lines go to Yahoo (%d under USD %.1fm/day not "
             "sent, %d unmeasured sent)", len(df), astats["pregate_adv_dropped"],
             cfg.min_adv_usd / 1e6, astats["pregate_adv_unmeasured"])

    # 4. fundamentals: one yfinance .info per line
    log.info("fetching fundamentals for %d tickers...", len(df))
    snap = prov.snapshot(df["ticker"].tolist(), asof)
    df = df.merge(snap, on="ticker", how="left")
    n_priced = int(df["market_cap_local"].notna().sum())
    log.info("  %d of %d lines priced by Yahoo", n_priced, len(df))
    # Refuse rather than screen a thinned market. With the turnover
    # pre-gate on, every measured line trades, so the bar is high
    # (hk_filters.priced_share); suspended lines are the healthy misses.
    got, base_n, share, need = HF.priced_share(df, cfg)
    ustats.update({"priced_of_base": got, "priced_base": base_n})
    if share < need:
        log.error("only %d of %d lines priced (%.0f%%, need %.0f%%). Yahoo is "
                  "rate-limiting; the cache has kept what arrived, so re-running in a "
                  "few minutes will fill the rest. Refusing to screen a partial "
                  "universe.", got, base_n, 100 * share, 100 * need)
        return 2
    df, mstats = HF.repair_market_caps(df)
    ustats.update({"priced": n_priced,
                   "mcap_via_fast_info": int((df["mcap_source"] == "fast_info").sum()),
                   **mstats})

    # 5. Yahoo's classification, and the hygiene that needs it
    df = HF.attach_classification(df)
    df, ustats = HF.apply_yahoo_filters(df, cfg, ustats)
    drops = sum(v for k, v in ustats.items() if k.startswith("dropped_"))
    ustats["after_hk_filters"] = len(roster) - drops
    log.info("after Hong Kong hygiene: %d priced lines", len(df))

    # 6. size gate on Yahoo's market cap - all shares, A and H
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * usd_per_hkd
    pre = df[df["market_cap_usd"] >= cfg.min_market_cap_usd].copy()
    ustats["cleared_market_cap"] = len(pre)
    log.info("%d of %d lines cleared USD %.0fm", len(pre), len(df),
             cfg.min_market_cap_usd / 1e6)
    if pre.empty:
        print("Nothing cleared the size gate.")
        return 0

    # 7. liquidity: the pre-gate's panel, downloaded again only for lines it
    #    has no number for (all of them with --skip-liquidity or
    #    --no-pregate), so the measured value reaches the page either way.
    for c in ("adv_local", "adv_sessions"):
        if c not in pre.columns:
            pre[c] = np.nan
    need = pre.loc[pre["adv_local"].isna(), "ticker"].tolist()
    if need:
        log.info("fetching %d days of prices for %d names...", cfg.adv_lookback_days, len(need))
        adv = prov.liquidity(need, cfg.adv_lookback_days).set_index("ticker")
        for c in ("adv_local", "adv_sessions"):
            if c in adv.columns:
                pre[c] = pre[c].fillna(pre["ticker"].map(adv[c]))
    pre["adv_usd"] = pd.to_numeric(pre["adv_local"], errors="coerce") * usd_per_hkd

    # 7b. one line per issuer (Swire A / B) - before the gate, so the traded
    #     line is kept whichever of them clears it.
    pre, lstats = HF.keep_one_line_per_issuer(pre)
    ustats.update(lstats)
    if not a.skip_liquidity:
        pre = pre[pre["adv_usd"] >= cfg.min_adv_usd]
    ustats["cleared_size_liquidity"] = len(pre)
    log.info("%d cleared size/liquidity", len(pre))
    if pre.empty:
        print("Nothing cleared the size and liquidity gates.")
        return 0

    # 8. control flag (Yahoo's insider holding)
    pre, cstats = HF.apply_control(pre, cfg)
    ustats.update(cstats)

    # 9. filed statements, survivors only (invariant 7)
    if not (a.no_financials and a.no_history):
        from providers_hk import fetch_statements
        log.info("fetching filed statements for %d names...", len(pre))
        st = fetch_statements(pre, cfg, asof)
        if len(st.columns) > 1:
            pre = pre.merge(st, on="ticker", how="left")

    # 10. pyramids - a parent consolidating a listed subsidiary. Needs the
    #     filed revenue, so it runs after the statements and before the peer
    #     medians, which must not count one set of earnings twice.
    if "rev_y1" in pre.columns:
        pre, pystats = HF.flag_pyramids(pre, cfg)
        ustats.update(pystats)
    if a.no_financials:
        pre = pre.drop(columns=[c for c in pre.columns
                                if c.startswith(("rev_", "op_", "ebitda_", "np_",
                                                 "fin_years", "fin_n"))])

    # 11. .info onto one basis: splits first (its test reads filed equity in
    #     the filing's currency), then the currency repair.
    if "share_basis_g" in pre.columns:
        pre, sstats = HF.restate_info_for_splits(pre)
        ustats.update(sstats)
    foreign = pre[pre["fin_ccy"].notna() & pre["fin_ccy"].ne(pre["currency"])]
    qeq = prov.quarterly_equity(foreign["ticker"].tolist()) if not foreign.empty else {}
    spot = prov.hkd_per(foreign["fin_ccy"].unique())
    pre, fxstats = HF.repair_foreign_reporters(
        pre, spot, prov.hkd_per_ttm(foreign["fin_ccy"].unique(), spot), qeq)
    ustats.update(fxstats)

    # 12. screen

    # Cash flow (P/FCF, shareholder yield) and the one-off guard - both
    # before the screen: P/FCF is a peer metric, and a capped P/E must be
    # what the peer medians are built from.
    if "lf_fcf" in pre.columns:
        pre, cfstats = HF.add_cashflow_metrics(pre)
        ustats.update(cfstats)
    pre, ostats = HF.apply_oneoff_guard(pre, cfg)
    ustats.update(ostats)

    res, stats = run_screen(pre, usd_per_hkd, cfg)
    if res.empty:
        print("Nothing survived screening.")
        return 0
    res = HF.add_quality_context(res)
    res = HF.add_valueup_flags(res)
    res = HF.flag_ttm_vs_filed(res)
    if cfg.min_roe_pct > 0:
        res, roestats = HF.apply_roe_gate(res, cfg)
        stats = {**stats, **roestats}
    else:
        res["roe_ok"], res["roe_tier"] = True, ""

    res, absstats = HF.apply_absolute_screen(res, cfg)
    stats = {**stats, **absstats}

    if not a.no_history and "hist_pbr" in res.columns:
        res, hstats = HF.apply_history_screen(res, cfg)
        stats = {**stats, **hstats}

    res["name_exchange"] = res["name"]
    yn = res.get("yf_name", pd.Series(index=res.index, dtype=object))
    res["name"] = [display_name(y if isinstance(y, str) else "", g)
                   for y, g in zip(yn, res["name_exchange"])]

    funnel = {**ustats, **stats}
    print("\n--- funnel ---")
    for k, v in funnel.items():
        print(f"  {k:<28} {v}")

    out = res if a.all else res[res["passes_any"]]
    cols = [c for c in HF.hk_output_columns(cfg) if c in res.columns]
    out[cols].to_csv(a.out, index=False, encoding="utf-8-sig")

    hits = res[res["passes_any"]]
    print(f"\n--- {len(hits)} stock(s) passing at least one screen ---")
    if not hits.empty:
        show = hits[["symbol", "name", "industry", "market_cap_usd",
                     "trailing_pe", "price_to_book", "roe_pct", "div_yield",
                     "avg_discount", "screen"]].head(50).copy()
        show["mcap_$m"] = (show.pop("market_cap_usd") / 1e6).round(0).astype("Int64")
        show["avg_discount"] = show["avg_discount"].map(
            lambda x: f"{x:.1%}" if pd.notna(x) else "")
        show["name"] = show["name"].str.slice(0, 28)
        show["industry"] = show["industry"].str.slice(0, 24)
        print(show.to_string(index=False))

    meta = {
        "asof": asof, "source": "hkex+yfinance", "board": "ALL",
        "roster_asof": prov.roster_asof,
        "cmd": "python main_hk.py " + " ".join(sys.argv[1:]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "usd_per_hkd": round(usd_per_hkd, 6),
        "funnel": funnel,
        "thresholds": {
            "min_mcap_usd": cfg.min_market_cap_usd,
            "min_adv_usd": 0 if a.skip_liquidity else cfg.min_adv_usd,
            "skip_liquidity": bool(a.skip_liquidity),
            "discount": cfg.discount_threshold,
            "min_metrics": cfg.min_metrics_passing,
            "min_peers": cfg.min_peers,
            "min_valid_metrics": cfg.min_valid_metrics,
            "min_roe_pct": cfg.min_roe_pct,
            "roe_good_pct": cfg.roe_good_pct,
            "abs_max_pbr": cfg.abs_max_pbr,
            "abs_max_ev_ebitda": cfg.abs_max_ev_ebitda,
            "abs_require_roe": cfg.abs_require_roe,
            "abs_financials_pbr_only": cfg.abs_financials_pbr_only,
            "abs_require_pbr_vs_roe": cfg.abs_require_pbr_vs_roe,
            "abs_cost_of_equity_pct": cfg.abs_cost_of_equity_pct,
            "abs_min_div_yield": cfg.abs_min_div_yield,
            "payout_basis": "shareholder_yield",
            "oneoff_guard": cfg.oneoff_guard,
            "oneoff_ratio": cfg.oneoff_ratio,
            "metrics": list(cfg.metrics),
            "hist_min_discount": cfg.hist_min_discount,
            "hist_min_metrics": cfg.hist_min_metrics,
            "hist_min_years": cfg.hist_min_years,
        },
    }
    meta_path = os.path.splitext(a.out)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {a.out} and {meta_path}")

    if a.dashboard:
        try:
            from dashboard import build_dashboard
            build_dashboard(a.out, meta_path, a.dashboard)
            print(f"wrote {a.dashboard}   <- open this")
        except Exception as e:                            # noqa: BLE001
            log.error("dashboard build failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
