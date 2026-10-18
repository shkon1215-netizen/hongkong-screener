# CLAUDE.md — Hong Kong (HKEX) Valuation Screener

Seventh market after Korea, the UK, Japan, Germany, Poland and France, built
on France's copy because France had already combined what the others
learned. Same maths (`screener.py`), same three screens (vs peers, outright,
vs own history), same thresholds except the liquidity gate. Universe: HKEX
Main Board + GEM (2,836 equity and REIT lines on 2026-10-14).

## Run order

```bash
python test_hongkong.py   # offline, must pass
python check_setup.py     # nine live checks
python main_hk.py -v      # full run, ~10 min cold (.info x1,300, statements x570), ~2 min warm
python serve.py           # dashboard with working Refresh (dashboard.cmd)
python sync_dashboard_from_korea.py ../Korea/dashboard.py   # dashboard.py is generated
```

```bash
python main_hk.py --min-adv 4e6          # the cross-market liquidity bar
python main_hk.py --include-property     # keep landlords and developers
python main_hk.py --include-secondary    # keep -S lines (Trip.com, Manulife)
python main_hk.py --no-pregate           # send all 2,700 lines to Yahoo (~3x slower)
```

## Files

| File | Role |
|---|---|
| `screener.py` | Core engine, Korea's maths. FX leg is USD per HKD. Do not edit the maths here first. |
| `config_hk.py` | Thresholds, bounds, HKEX categories, name markers (-S, -W, -OLD), temporary code ranges, property industries, named holdcos. |
| `providers_hk.py` | HKEX List of Securities, HKEX equities feed (market caps), Yahoo snapshot, liquidity, FX (spot and trailing-year average), filed statements (UK's `build_statement_record`, unchanged). |
| `hk_filters.py` | Roster hygiene, pre-gate, market-cap repair, Yahoo hygiene, one line per issuer, control, pyramids, the currency repair, then the shared screens. |
| `main_hk.py` | CLI. Orders the gates so per-ticker work runs last. |
| `dashboard.py` | Generated — do not edit. Run `sync_dashboard_from_korea.py` (72 substitutions, each must match once). |
| `serve.py`, `build_site.py`, `dashboard.cmd` | Local server, GitHub Pages site, double-click launcher. |
| `check_setup.py`, `test_hongkong.py` | Live pre-flight; offline tests with planted traps. Keep green. |

## Where each piece came from

| piece | from | Hong Kong change |
|---|---|---|
| roster from the operator | Germany, France | HKEX's List of Securities xlsx, one request |
| cheap pre-gate on the operator's caps | Korea, Japan | HKEX's equities feed; **H-shares exempt** |
| classification | UK (Yahoo industry → sector) | HKEX publishes no whole-market HSICS file |
| market-cap repair | Germany (fast_info), Poland (exchange cap) | HKEX's cap fills a blank — never for an H-share |
| one line per issuer | Germany | RMB counters by HKEX's currency field; Swire A / B by name |
| foreign secondaries | Germany, France | HKEX's own `-S` marker, DRs, trading-only US shares |
| investment vehicles | Germany, Poland | HKEX sub-category "Investment Companies" (Chapter 21) |
| landlords | Germany | Yahoo industry: Real Estate - Development / Diversified |
| currency repair | Poland → France | EPS tested at the year's average rate; renminbi never "repaired" |
| pyramids, holdcos, control | France | pyramids also need the same sector |
| splits, TTM-vs-filed flag | UK, Germany | none |

## Hong Kong invariants

1. **Only the HKD counter is screened.** 24 companies have an RMB counter
   (Tencent 00700 / 80700, `-R` names). HKEX's Trading Currency column
   decides, not the name.
2. **Temporary counters are dropped by the roster**, two kinds: `-OLD` lines
   during a board-lot change, and a second line under an identical name in
   HKEX's temporary ranges (02900–02999, 08500–08599) during a share
   consolidation — BILLION IND 02943 beside 02299. Their market caps sit on
   the wrong share count for weeks (HKEX shows SG Group's two lines 10x
   apart). 17 dropped on 2026-10-14.
3. **The pre-gate never judges an H-share.** HKEX's market cap for a
   mainland-incorporated company is the H tranche only — PetroChina HK$217bn
   on HKEX, HK$1.88tn on Yahoo. Every CNE ISIN (538 lines) goes to Yahoo;
   other lines go only if HKEX's cap is at least a quarter of the floor.
   1,300 of 2,750 lines are priced, ~6 minutes cold.
4. **Yahoo's market cap on an H-share is all shares, A and H** (ICBC: H price
   × 356bn shares). That is the company's size, it matches the filed share
   count, so the history screen's basis check holds. `check_setup.py`
   asserts it. An H-tranche-only cap would fail the basis check, not
   invent a 4x premium (tested).
5. **The currency repair is France's, re-measured, and two things differ.**
   487 of 566 survivors report in another currency (renminbi, US dollars).
   On HK lines Yahoo's market cap, EV, EPS and book value per share are in
   HKD; EBITDA and net income are in the reporting currency. So P/E and P/B
   are right as stated and **EV/EBITDA is wrong on every one** — AIA reads
   86.5 (HKD EV / USD EBITDA). 365 repaired on the reference run.
   - **EPS is tested against the trailing-year average rate, not spot.**
     The renminbi rose from 1.09 to 1.17 HKD over the year; Yahoo's TTM EPS
     sums four quarters converted at four rates. Against spot the EPS ratio
     sat at 0.95 with a quarter of names near the midpoint between
     hypotheses; against the average its IQR is 0.96–1.01.
   - **Book value is tested against spot**: k / spot = 1.000 wherever the
     interim is Yahoo's latest period.
   - **The reporting-currency reading is only accepted where the currencies
     are ≥1.5x apart** (USD, EUR, SGD, JPY). Not one of 67 dollar reporters
     had EPS or book on the dollar basis. For the renminbi (1.14–1.17) the
     hypotheses overlap, so Yahoo's measured convention is assumed — and if
     it were ever wrong, the multiple would come out ~15% *dearer*, never
     cheaper.
   - **An EV outside France's [0.25, 4] band is kept when filed net debt
     corroborates it.** Indebted SOEs at a fifth of book carry EVs of
     5–12x market cap (China Railway 8.2, CNBM 11.9); Yahoo's figure matched
     market cap + filed debt − cash + minorities in HKD to within 12%, and
     France's band had refused all of them.
   - 60 P/Bs and 23 P/Es are refused. Not currency: recent IPOs whose equity
     jumped since filing (Z.AI, MiniMax), perpetual capital Yahoo's book
     leaves out (Datang, Huaneng at k ≈ 0.46), stale filings (Pop Mart's
     newest filed year is 2024). Missing, never guessed.
6. **Pyramids need the same Yahoo sector.** France's three tests paired
   Yankuang Energy (coal, heavy minorities) with Muyuan Foods (hogs) —
   revenue 144.9bn vs 144.1bn, operating profit within 5% — and dropped the
   coal miner. Hang Lung Group / Hang Lung Properties is the real shape
   (both excluded as property by default).
7. **Holdcos by a named list.** Nearly every HK company is "... HOLDINGS",
   so no name token works. `KNOWN_HOLDCOS` by stock code: Swire Pacific A/B,
   First Pacific, Hang Lung Group, Guoco. Add to it rather than invent a
   heuristic.
8. **Financials = Yahoo sector "Financial Services"**; real estate is its
   own sector, so no carve-out.
9. Germany's invariants 2, 4, 5, 7, 8, 11 and 14 hold unchanged.

## Excluded by default (2026-10-14)

| class | rule | dropped |
|---|---|---|
| RMB / USD counters | Trading Currency ≠ HKD | 27 |
| temporary counters | `-OLD`, or same name in 29xx / 85xx | 17 |
| investment companies | sub-category, Chapter 21 | 20 |
| REITs | HKEX category | 10 |
| secondary listings, DRs, trading-only | `-S` / `-SW`, sub-category | 19 |
| landlords and developers | Yahoo industry | 60 (28 past USD 600m) |
| second share class | Swire Pacific B | 1 |

**Property** is the judgement call. The 28 that clear USD 600m have median
P/B 0.33 (New World 0.09, Longfor 0.18, CK Asset 0.39) and 9 of the largest
20 have no P/E — HKAS 40 revaluations now run through earnings as losses.
The book is an appraisal and the discount to it is standing. Property
management (Real Estate Services: China Overseas Property, Poly Property
Services) is an operating business and stays. `--include-property` keeps
them all.

## Liquidity gate: USD 1m (not 4m), measured 2026-10-14

| ADV floor | names | relative / absolute / history / any |
|---|---|---|
| none | 691 | 121 / 89 / 53 / 186 |
| USD 1m (default) | 566 | 104 / 75 / 44 / 157 |
| USD 2m | 467 | 89 / 63 / 40 / 134 |
| USD 4m | 358 | 69 / 42 / 34 / 108 |

HKEX is the only venue, so this is the whole market's volume (unlike
Xetra / Euronext). Between 1m and 4m the gate removes ordinary large
H-shares whose volume lives on the A side: CRCC, CCCC, Zoomlion, Shanghai
Pharma, CIMC, Sinotrans, Huadian — USD 5–9bn companies — plus Prudential,
whose main market is London. Under 1m goes what should: H-share banks that
do not trade (Guangzhou Rural, Weihai: zero), RUSAL, Guoco, and GEM lines
with consolidation-corrupted caps (SG Group "USD 2.2bn" on USD 15k a day).
France's and Poland's finding, and their number.

## Calibration, measured rather than inherited

566 names past the default gates, hygiene applied, 2026-10-14:

| | p10 | p25 | median | p75 | p90 |
|---|---|---|---|---|---|
| P/E | 6.22 | 8.20 | **12.03** | 20.40 | 39.46 |
| P/B | 0.48 | 0.71 | **1.35** | 2.71 | 4.97 |
| EV/EBITDA | 3.87 | 5.75 | **8.39** | 15.09 | 27.27 |
| ROE % | −6.11 | 4.05 | **8.87** | 13.75 | 21.32 |
| Yield % | 0.00 | 0.00 | **2.33** | 5.12 | 6.92 |

**P/B < 1 passes 32%, EV/EBITDA < 8 passes 33%** — Korea's levels land on
the cheapest third, as in Korea itself. Which test binds: 127 names are below
book and below 8x (or a financial below book); ROE ≥ 5% leaves 92, fair P/B
81, the dividend floor 75. No test is a no-op. Cost of equity 8 / 9 / 10 /
12% gives 82 / 79 / 75 / 58 — 10% stays.

**One page.** One GEM name survives the gates; Main Board and GEM share a
page with a board filter.

## Results, reference run (2026-10-14 close)

```
2,836 lines (2,484 Main, 312 GEM, 40 REIT / investment co / DR / trading-only)
  2,683 after roster + Yahoo hygiene
  1,300 sent to Yahoo by the pre-gate (538 H-shares), 1,285 priced
    699 cleared USD 600m
    566 cleared USD 1m/day
    104 cheap vs peers (after ROE ≥ 5%)
     75 cheap outright (32 financials via the P/B + ROE carve-out)
     44 cheap vs own history (18 found by nothing else)
    157 passing at least one screen (73 of them H-shares)
```

Browser verdict at default thresholds equals Python's: 157 / 104 / 75 / 44,
and every per-test count (P/B < 1 183, EV < 8 185, fair 145, yield 291,
ROE 332, carve-out 32).


## Cash flow, shareholder yield and the one-off guard (added 2026-10-18)

Added to all eight screeners at once, from the US build. Source: yfinance's annual cash-flow statement, fetched with the income statement (`cashflow_fields`; `STATEMENTS_CACHE_VERSION` 5).

- **P/FCF is a fourth peer metric** - market value over the latest filed
  free cash flow (operating cash flow less capex), in the reporting
  currency. Missing for financials, as EV/EBITDA is (invariant 6), and for a
  negative free cash flow. The relative rule is unchanged: 20% below peers
  on at least two of the metrics a company has, so with four metrics a name
  has more ways to qualify.
- **The outright screen's payout test is the shareholder yield** -
  dividends plus buybacks less shares issued, over market value. The 2%
  floor is the user's, unchanged. A company buying back only what it issues
  to employees gets no credit; where no cash-flow statement was filed the
  dividend yield stands in (`abs_payout_from_dividends_only` counts them).
  Measured in the US: the dividend floor alone cut 89 cheap, profitable
  names to 46.
- **One-off guard.** On the last four quarters' net and operating profit, summed from Yahoo's quarterly income statement (the latest filed year where four quarters are not available, as for half-yearly reporters). NOT .info's operatingMargins, which is one quarter's margin: on the first US run it gave 141 flags out of 2,285, most of them false (AECOM, Intuit, Qualcomm): net profit above 1.5x operating profit
  after a 16.5% tax rate, where the earlier filed years do NOT show the same
  gap, is treated as below-the-line (a tax credit, an investment gain, an
  asset sale). The row's P/E and EPS - and so its ROE - are restated on
  operating profit after tax BEFORE the peer medians are built; an operating
  loss under a reported profit leaves no P/E. Tagged `one-off`, never
  dropped. A gap present every year (an equity-method holding, a trading
  house) is structural and spared; financials are exempt. Measured cases:
  Lyft 21.9x (tax allowance release), Rigel 5.2x, Alphabet 2.2x (gains on
  stakes); ordinary companies 1.0-1.2x (Microsoft 1.19, Apple 1.13);
  Mitsubishi Corporation 2.1x every year (spared). `--no-oneoff-guard`
  scores earnings as reported.

## Known gaps (ranked by value of fixing)

1. **No HSICS.** Peers are Yahoo's industry (299 of 565 scored within it on
   P/E) falling back to its sector. HKEX shows HSICS per stock on its web
   pages only.
2. **No state flag.** Most H-shares are state-controlled through A-shares or
   unlisted domestic shares, which Yahoo's insider figure does not see. The
   `h` tag is the proxy.
3. **AH premium not read.** The A-share price of an H-share company is not
   fetched; an H-share's discount to its own A-share is a market-access
   artefact, not a valuation, and the screen does not pretend otherwise.
4. **Trailing one-offs.** 3SBio's P/E 3.9 is a 2025 licensing receipt from
   Pfizer; it is in the filed year too, so `ttm vs fy` does not fire.
5. **Consolidation-corrupted market caps** on names that keep their code
   (SG Group, Capital Finance) are only removed by the liquidity gate; with
   `--skip-liquidity` they are in.
6. Four filed years for the history screen; trailing multiples only.

## Likely first failures

- **Yahoo throttles silently.** The run refuses to screen under half priced;
  the per-day cache fills gaps on re-run.
- **HKEX's equities feed moves its token.** `token_from_page` reads it off
  the equities page; on failure the run continues without the pre-gate
  (~3x slower) and says so. `check_setup.py` checks it.
- **HKEX changes the List of Securities layout.** Header found by its
  "Stock Code" cell; `check_setup.py` asserts Tencent, HSBC, ICBC, Alibaba
  and the presence of RMB counters.
- **The statements cache stores derived records.** Bump
  `STATEMENTS_CACHE_VERSION` when `build_statement_record` changes.
- **pandas 3.x** — pinned `<3`.

## Keeping in step with Korea

```bash
python sync_dashboard_from_korea.py ../Korea/dashboard.py
```

Every substitution must match exactly once; misses exit 1. Then grep for
Korean text, KOSPI, PER/PBR and `ticker`, and confirm in a browser that the
page's verdict equals the Python funnel at default thresholds.

## Publishing

`.github/workflows/screen.yml` runs at 09:30 UTC on weekdays (HKEX closes
16:10 HKT = 08:10 UTC; Hong Kong keeps no daylight saving), builds `site/`
and deploys to GitHub Pages, with a retry after a pause for Yahoo
throttling.

Live: https://shkon1215-netizen.github.io/hongkong-screener/. Unlisted -
`noindex` plus a blanket `robots.txt` - but the repo is public, which free
Pages requires. No screen output is committed.

**HKEX and Yahoo both answer GitHub's runners** - confirmed on the first
working run, 2026-10-14: the List of Securities and the equities feed
(2,796 caps) both read, 1,284 of 1,299 lines priced, no retry needed, ~11
minutes cold. The funnel matched the local run to within the day's price
moves (701 / 568 / 158 against 699 / 566 / 157). The run before it failed
on a missing `openpyxl` - requirements.txt had come from France, which
reads a CSV. If the roster step ever fails in CI while working locally,
suspect HKEX blocking the runner range before a code change.

A docs-only push does not trigger a run (`paths-ignore: **.md`); start one
with `gh workflow run screen.yml`.

## Interpretation

Sort by `avg_discount`, then read `roe_pct` immediately, then look for tags.
`h` means the multiples are the H-share's and an A-share sibling usually
trades dearer; `fx` says what the currency repair did; `close` means the
free float is the minority; `holdco` means a portfolio discount may be
structural. Low P/B with low ROE is arithmetic, not opportunity.

Research tool, not investment advice.
