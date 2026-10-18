# Hong Kong (HKEX) Relative Valuation Screener

**Live:** https://shkon1215-netizen.github.io/hongkong-screener/ · rebuilt each
weekday after the HKEX close

Finds Hong Kong-listed stocks trading at a discount to their industry peers
on P/E, P/B and EV/EBITDA — and, independently, stocks that are cheap in
absolute terms, and stocks that are cheap against their own filed history.
Each row also carries three years of revenue, EBITDA and net profit.

**Defaults:** market cap ≥ USD 600M · median daily traded value ≥ USD 1M on
HKEX · ≥20% below peer median on ≥2 of 4 metrics (P/E, P/B, EV/EBITDA, P/FCF) · ROE ≥ 5%.

The seventh market after [Korea](https://github.com/shkon1215-netizen/korea-screener),
the UK, Japan, Germany, Poland and France. `screener.py` is the same maths;
what is local is the data layer, the universe hygiene and the currency repair.


**Also:** the outright screen's 2% payout floor counts buybacks (shareholder
yield = dividends + buybacks − issuance), and trailing profit far above
operating profit — a tax credit, an investment gain — is capped at
operating profit after tax before scoring (tagged `one-off`).

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
python test_hongkong.py  # offline logic check, no network needed
python check_setup.py    # tests every live call individually
python main_hk.py -v     # full run, ~10 minutes cold, ~2 warm
```

```bash
python main_hk.py --min-adv 4e6                # the cross-market liquidity bar
python main_hk.py --skip-liquidity             # size alone gates
python main_hk.py --discount 0.15 --min-metrics 1
python main_hk.py --include-property --all     # see why landlords are off
python main_hk.py --fx 0.1275                  # pin USD per HKD
```

Results cache to `.hk_cache/` per session date, so re-runs are fast — and so
a throttled Yahoo cannot silently shrink the universe.

## The dashboard

Every run writes a self-contained `hk_dashboard.html`. Open it directly, or
run `python serve.py` (on Windows, double-click `dashboard.cmd`) to make the
**Refresh** button work. Every threshold on the page is re-evaluated in the
browser; peer medians and sub-floor market caps cannot be, and the page says
so.

## Where the data comes from

| | source |
|---|---|
| roster, board, category, trading currency | HKEX's own *List of Securities* — every Main Board and GEM line |
| pre-gate market caps | HKEX's equities feed, one request for the whole market |
| multiples, EPS, book, yield, industry | Yahoo `.info` on the HK line, `fast_info` when the market cap is blank |
| liquidity | one batched Yahoo price download, 60-session median traded value |
| filed statements | Yahoo annual income statement and balance sheet |

## Hong Kong traps this handles

**One company, two counters.** Tencent trades as 00700 in HKD and 80700 in
RMB; temporary counters open during board-lot changes and share
consolidations. Only the permanent HKD line is screened.

**H-shares.** HKEX's market cap for a mainland company is its H tranche only
(PetroChina: an eighth of the company), so H-shares are never pre-gated on
it. Yahoo's cap is all shares, A and H — the company's size.

**Most of the market reports in another currency.** Tencent and ICBC report
in renminbi, HSBC and AIA in dollars. Yahoo divides an HKD enterprise value
by a reporting-currency EBITDA (AIA: 86.5), so EV/EBITDA is repaired; EPS and
book value are tested per company against the rate Yahoo actually used.

**Secondary listings** (`-S`: Trip.com, Manulife), depositary receipts,
Chapter 21 investment companies and REITs are excluded by HKEX's own labels.

**Landlords and developers** are excluded by default: an appraised book at a
standing discount (median P/B 0.33) and revaluations through earnings.

**Swire Pacific's A and B shares** are one company: only the traded line is
kept.

Research tool, not investment advice.
