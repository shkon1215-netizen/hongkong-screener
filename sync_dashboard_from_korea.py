"""Re-derive the Hong Kong dashboard from the Korea build's current dashboard.py.

    python sync_dashboard_from_korea.py ../Korea/dashboard.py

The Hong Kong dashboard is not a fork that drifts: it is Korea's dashboard.py
with a fixed list of substitutions applied. Most are France's, which are
mostly Germany's (P/E and P/B labels, same-basis per_now/pbr_now for the
own-history screen, the reporting currency in growth tooltips, four filed
years instead of five, the undefined guard in cell(), one page rather than
one per board, closely-held / holdco / listed-parent / currency-repair tags,
a hide-closely-held toggle); the rest are Hong Kong's (the five-digit HKEX
code, financials by Yahoo's sector, H-share and weighted-voting tags, Hong
Kong notes). When Korea gains a feature, run this.

Every substitution must match exactly once. Anything that no longer matches is
printed and the exit code is 1 - Korea's text moved, and that substitution
needs updating by hand rather than being silently skipped. Afterwards, grep
the result for Korean text, KOSPI, PER/PBR and "ticker" to catch anything new
Korea added that this list does not know about yet, and re-check that the
browser's verdict matches the Python funnel at default thresholds.
"""
import io
import sys

if len(sys.argv) != 2:
    sys.exit("usage: python sync_dashboard_from_korea.py <path to Korea dashboard.py>")
SRC = sys.argv[1]
P = "dashboard.py"
s = io.open(SRC, encoding="utf-8").read()


PAIRS = [
    # ---- module docstring and imports --------------------------------------
    ("Reads the CSV that main_kr.py writes", "Reads the CSV that main_hk.py writes"),
    ("re-run main_kr.py to refresh it in place.", "re-run main_hk.py to refresh it in place."),
    ("import numpy as np\nimport pandas as pd\n",
     "import numpy as np\nimport pandas as pd\n\n"
     "# Financials by Yahoo's sector, the same test the screen used - the page's\n"
     "# carve-out must agree with Python's or its verdict drifts.\n"
     "from config_hk import is_financial\n"),

    # HKEX codes are five digits with leading zeros (00700); read as numbers
    # they would lose them.
    ('dtype={"ticker": str})', 'dtype={"ticker": str, "symbol": str, "code": str, "isin": str})'),

    # ---- funnel + table ----------------------------------------------------
    ('("listings", "Corporate lines", "ETFs, ETNs and funds already removed"),',
     '("listings", "Listed lines", "HKEX equity and REIT lines, Main Board + GEM"),'),
    ('("after_korea_filters", "After share-class hygiene", None),',
     '("after_hk_filters", "After universe hygiene", None),'),
    ('("cleared_size_liquidity", "Cleared size gate", None),',
     '("cleared_size_liquidity", "Cleared size + liquidity", None),'),
    ('("ticker", "Code", "l"), ("name", "Name", "l"), ("industry", "업종 industry", "l"),',
     '("symbol", "Code", "l"), ("name", "Name", "l"), ("industry", "Industry", "l"),'),
    ('("mcap_musd", "Cap $m", ""), ("trailing_pe", "PER", ""), ("price_to_book", "PBR", ""),',
     '("mcap_musd", "Cap $m", ""), ("trailing_pe", "P/E", ""), ("price_to_book", "P/B", ""),'),
    ('("hist_avg_disc", "vs own 5y", ""),', '("hist_avg_disc", "vs own history", ""),'),
    ('("abs_pbr_ok", "PBR below {abs_max_pbr:g}"),', '("abs_pbr_ok", "P/B below {abs_max_pbr:g}"),'),
    ('("abs_pbr_vs_roe_ok", "PBR below fair value (ROE ÷ {abs_cost_of_equity_pct:g}% CoE)"),',
     '("abs_pbr_vs_roe_ok", "P/B below fair value (ROE ÷ {abs_cost_of_equity_pct:g}% CoE)"),'),

    # ---- row payload -------------------------------------------------------
    ('            "ticker": str(r.get("ticker", "")),\n',
     '            # The HKEX code, not the Yahoo one: 00700 is what a Hong Kong\n'
     '            # reader looks up, 0700.HK is a vendor detail. This key must stay\n'
     '            # in step with TABLE_COLS - see the guard in cell().\n'
     '            "symbol": str(r.get("symbol", "") or r.get("ticker", "")),\n'),
    # Main Board and GEM are the two boards, so Korea's board field and
    # filter are kept as they are.
    ('            "fin": bool(str(r.get("sector", "") or "").lower().find("financial") >= 0),',
     '            "fin": bool(is_financial(r.get("sector", "") or "", r.get("industry", "") or "")),\n'
     '            # Closely held: insiders and strategic holders own a majority\n'
     '            # (Yahoo\'s figure) - see hk_filters.apply_control. A flag.\n'
     '            "ctrl": "" if pd.isna(r.get("ctrl_pct")) or r.get("is_controlled") != True else\n'
     '                    f"{float(r.get(\'ctrl_pct\')):.0f}%",\n'
     '            # What a named holding company holds - config_hk.KNOWN_HOLDCOS.\n'
     '            "hold": "" if pd.isna(r.get("holdco_note")) else str(r.get("holdco_note") or ""),\n'
     '            # The listed parent that consolidates this company (Hang Lung\n'
     '            # Group over Hang Lung Properties) - hk_filters.flag_pyramids.\n'
     '            "pyr": "" if pd.isna(r.get("pyramid_child_of")) else str(r.get("pyramid_child_of") or ""),\n'
     '            # What the currency repair did to this row, if anything - see\n'
     '            # hk_filters.repair_foreign_reporters.\n'
     '            "fxn": "" if pd.isna(r.get("fx_note")) else str(r.get("fx_note") or ""),\n'
     '            # Trailing profit against a filed loss - see hk_filters.flag_ttm_vs_filed.\n'
     '            "ttmx": bool(r.get("ttm_vs_filed_loss") == True),  # noqa: E712\n'
     '            # An H-share: a mainland-incorporated company\'s Hong Kong tranche.\n'
     '            "hsh": bool(r.get("is_h_share") == True),  # noqa: E712\n'
     '            # Weighted voting rights (-W): the founders\' shares carry the vote.\n'
     '            "wvr": bool(r.get("is_wvr") == True),  # noqa: E712'),
    ('            "evx_now": _f(r.get("evx_now"), 6),\n',
     '            "evx_now": _f(r.get("evx_now"), 6),\n'
     '            # Today\'s P/E and P/B on the SAME basis as the history (market\n'
     '            # value over the latest filing), which is not the basis of the\n'
     '            # trailing_pe/price_to_book columns - see add_history_now.\n'
     '            # These are what the history screen thresholds against, so\n'
     '            # they carry full precision like the multiples above.\n'
     '            "per_now": _f(r.get("per_now"), 6),\n'
     '            "pbr_now": _f(r.get("pbr_now"), 6),\n'
     '            # The reporting currency the 3-year figures are in. Not the quote\n'
     '            # currency: Tencent trades in HKD and reports in renminbi.\n'
     '            "fin_ccy": str(r.get("fin_ccy", "") or ""),\n'),
    ('            # Three-year history, oldest first, in 억원. The yearly values',
     '            # Three-year history, oldest first, in millions of the REPORTING\n'
     '            # currency (fin_ccy). The yearly values'),
    ('            # Own five-year history. The medians and today\'s values let the',
     '            # Own filed history. The medians and today\'s values let the'),

    # ---- drop labels -------------------------------------------------------
    ('''    for key, label in [("dropped_preferred", "우선주 preferred"),
                       ("dropped_reit", "리츠 REIT"),
                       ("dropped_spac", "스팩 SPAC")]:''',
     '''    for key, label in [("dropped_other_counter", "RMB / USD counters"),
                       ("dropped_temp_counter", "temporary counters"),
                       ("dropped_investment_co", "investment companies (Ch. 21)"),
                       ("dropped_reit", "REITs"),
                       ("dropped_foreign_secondary", "secondary listings and DRs"),
                       ("dropped_property", "landlords and developers"),
                       ("dropped_second_line", "second share-class lines"),
                       ("dropped_pyramid_parent", "parents consolidating a listed subsidiary"),
                       ("dropped_controlled", "closely held")]:'''),

    # ---- boards: one page ----------------------------------------------------
    ('''BOARD_FILES = {"KOSPI": "kr_dashboard.html",
               "KOSDAQ": "kq_dashboard.html",
               "BOTH": "krkq_dashboard.html"}''',
     '''# Hong Kong runs as ONE screen of the Main Board and GEM: almost nothing on
# GEM clears USD 600m, too few to carry a page of their own.
BOARD_FILES = {"ALL": "hk_dashboard.html"}'''),
    ('''# its own. KOSPI keeps the original name - renaming a published artifact makes
# it unrecognisable to anyone who bookmarked it.
BOARD_TITLES = {"KOSDAQ": "KOSDAQ Discount Screen"}''',
     '''# its own. One page here, so nothing to retitle.
BOARD_TITLES = {}'''),

    # ---- FX field ----------------------------------------------------------
    ('"krw_per_usd": meta.get("krw_per_usd"),', '"usd_per_hkd": meta.get("usd_per_hkd"),\n'
     '            "roster_asof": meta.get("roster_asof", ""),'),

    # ---- titles ------------------------------------------------------------
    ('head.replace("<title>Korea Discount Screen</title>",',
     'head.replace("<title>Hong Kong Discount Screen</title>",'),
    ('body.replace("<h1>Korea Discount Screen</h1>",', 'body.replace("<h1>Hong Kong Discount Screen</h1>",'),
    ('_HEAD = """<title>Korea Discount Screen</title>', '_HEAD = """<title>Hong Kong Discount Screen</title>'),
    ('<p class="eyebrow">KRX relative valuation</p>', '<p class="eyebrow">HKEX relative valuation</p>'),
    ('<h1>Korea Discount Screen</h1>', '<h1>Hong Kong Discount Screen</h1>'),

    # ---- fonts and line breaking -------------------------------------------
    # Names are shown in English (Yahoo's), so the Latin face is enough.
    ('family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@300;400;500;600;700&display=swap',
     'family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap'),
    ('--sans:"IBM Plex Sans KR", system-ui, -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;',
     '--sans:"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;'),
    ('''/* Korean breaks at any character by default, so 현대지에프홀딩스 shatters across
   four lines in a narrow column. keep-all breaks at word boundaries instead. */''',
     '''/* English names break at spaces first and mid-word only when a single word
   is wider than its column - keep-all would push it out of the table. */'''),
    ("  word-break:keep-all}", "  word-break:normal;overflow-wrap:anywhere}"),

    # ---- commands / search / filters ---------------------------------------
    ('<code id="cmd">python main_kr.py</code>', '<code id="cmd">python main_hk.py</code>'),
    ('placeholder="name, code or 업종"', 'placeholder="name, code or industry"'),
    ('$("cmd").textContent = M.cmd || ("python main_kr.py --board "',
     '$("cmd").textContent = M.cmd || ("python main_hk.py --board "'),
    ('    <label class="toggle"><input type="checkbox" id="nohold"> Hide holdcos</label>',
     '    <label class="toggle"><input type="checkbox" id="nohold"> Hide holdcos</label>\n'
     '    <label class="toggle"><input type="checkbox" id="nostate"> Hide closely held</label>'),
    ('  const onlyPass = $("onlypass").checked, noHold = $("nohold").checked;',
     '  const onlyPass = $("onlypass").checked, noHold = $("nohold").checked;\n'
     '  const noState = $("nostate").checked;'),
    ('    if (noHold && r.holdco) return false;',
     '    if (noHold && r.holdco) return false;\n'
     '    if (noState && r.ctrl) return false;'),
    ('["q","brd","scr","gmode","onlypass","nohold"].forEach(id =>',
     '["q","brd","scr","gmode","onlypass","nohold","nostate"].forEach(id =>'),

    # ---- history panel, selector and control labels ------------------------
    ('<h2>Cheap vs its own 5 years</h2>', '<h2>Cheap vs its own history</h2>'),
    ('<span class="hint">median of filed years</span>',
     '<span class="hint">median of 4 filed years</span>'),
    ('<option value="history">Cheap vs own 5y</option>',
     '<option value="history">Cheap vs own history</option>'),
    ('<div class="tg"><label for="t_hdisc">Below own 5y by at least %</label>',
     '<div class="tg"><label for="t_hdisc">Below own history by at least %</label>'),

    # ---- threshold controls ------------------------------------------------
    ('<div class="tg"><label for="t_pbr">PBR below</label>',
     '<div class="tg"><label for="t_pbr">P/B below</label>'),
    ('<div class="tg"><label for="t_per">PER below</label>',
     '<div class="tg"><label for="t_per">P/E below</label>'),
    ('id="t_fair"> Require PBR below fair value', 'id="t_fair"> Require P/B below fair value'),
    ('id="t_carve"> Financials qualify on PBR + ROE', 'id="t_carve"> Financials qualify on P/B + ROE'),
    ('tests.push(["PBR below " + T.pbr,', 'tests.push(["P/B below " + T.pbr,'),
    ('tests.push(["PER below " + T.per,', 'tests.push(["P/E below " + T.per,'),
    ('tests.push([`PBR below fair value (ROE ÷ ${T.coe}% CoE)`,',
     'tests.push([`P/B below fair value (ROE ÷ ${T.coe}% CoE)`,'),
    ('financials qualified on PBR and ROE ', 'financials qualified on P/B and ROE '),
    ('${hist.length} vs own 5y`},', '${hist.length} vs own history`},'),

    # ---- chips -------------------------------------------------------------
    ('["Source", M.source === "naver" ? "KIND + Naver" : "KRX"],',
     '["Source", "HKEX + Yahoo"],\n  ["Roster as at", M.roster_asof || "?"],'),
    ('["USD/KRW", M.krw_per_usd],',
     '["HKD per USD", M.usd_per_hkd ? (1 / M.usd_per_hkd).toFixed(4) : "?"],'),

    # ---- interpretation copy -----------------------------------------------
    ('''    <p><b>Cheap vs its own five years</b> compares today's PER, PBR and EV/EBITDA
    with the median of the company's last five filed years. It catches what the
    other two miss - a company that always traded at a premium and has just
    de-rated. Loss years drop out of the benchmark rather than dragging it, and
    fewer than three usable years means no benchmark at all. <b>One caution:</b>
    these are trailing multiples, so when earnings are surging the latest filing
    lags the price and a stock reads <i>expensive</i> against its history until
    the next filing catches up. A one-off gain does the opposite.</p>''',
     '''    <p><b>Cheap vs its own history</b> compares today's P/E, P/B and EV/EBITDA
    with the median of the company's last four filed years - four, not Korea's
    five, because that is all Yahoo holds for Hong Kong companies. It catches what
    the other two miss: a company that always traded at a premium and has just
    de-rated. Loss years drop out of the benchmark rather than dragging it, and
    fewer than three usable years means no benchmark at all.</p>
    <p><b>Today's multiples in that column are not the ones in the P/E and P/B
    columns.</b> Each past year is that year-end market value over that year's
    filed accounts, so today is measured the same way: today's market value over
    the latest filed year, on Yahoo's normalized earnings. The P/E column uses
    the last twelve months instead; comparing across the two would report the
    gap between two definitions as a discount. <b>One caution:</b> when earnings
    are rising, the latest filing lags the price and a stock reads
    <i>expensive</i> against its history until the next filing catches up.</p>'''),
    ('''    own 5 years</b> asks whether it is cheap against itself. Korea needs all
    three:''',
     '''    own history</b> asks whether it is cheap against itself. Hong Kong needs
    all three:'''),
    ('''how far below book it traded. Those names clear the absolute screen on PBR and
    ROE alone and are marked <span class="tag">pbr+roe</span>.</p>''',
     '''how far below book it traded. Those names clear the absolute screen on P/B and
    ROE alone and are marked <span class="tag">pbr+roe</span>.</p>'''),
    ('''<p><b>Low PBR with low ROE is not a discount.</b> It is a company not earning
    its cost of capital, priced accordingly. Much of what gets called the Korea
    Discount is this.''',
     '''<p><b>Low P/B with low ROE is not a discount.</b> It is a company not earning
    its cost of capital, priced accordingly. Much of what trades below book in
    Hong Kong is this.'''),
    ('''<p><b>업종 files holding companies under 기타 금융업.</b> That pools operating
    holdcos with bank holdcos in one peer group, and suppresses EV/EBITDA for
    both — enterprise value is meaningless for a bank, but not for an operating
    company. Holdcos are tagged so you can see which rows this touches.</p>''',
     '''<p><b>Hong Kong's structurally cheap lines are taken out first,</b> each
    by HKEX's own label: the renminbi counter of a dual-counter stock (Tencent
    trades as 00700 in HKD and 80700 in RMB - one company, one line kept);
    Chapter 21 investment companies, which trade at a standing discount to
    NAV; REITs; and secondary listings (<i>-S</i> names such as Trip.com and
    Manulife, plus depositary receipts), whose price is set in New York or
    Toronto. Landlords and developers go too: Hong Kong's landlords revalue
    their buildings through earnings and trade at a standing discount to an
    appraised book, and China's developers are priced on their credit. A
    second share class of one company (Swire Pacific's B shares) and a parent
    that consolidates a listed subsidiary would count one set of earnings
    twice, so they are dropped. Peer groups are Yahoo's industries, falling
    back to its sectors - HKEX publishes no whole-market classification
    file.</p>
    <p><b>Holding companies are tagged, not dropped.</b> Swire Pacific, First
    Pacific and Hang Lung Group trade at a standing discount to their stakes.
    A <span class="tag">holdco</span> tag says what the company holds; hover
    it. A <span class="tag">listed parent</span> tag marks a company whose
    parent is also listed and consolidates it.</p>
    <p><b>An <span class="tag">h</span> tag marks an H-share</b> - the Hong
    Kong tranche of a company incorporated on the mainland. Most also trade
    A-shares in Shanghai or Shenzhen, usually at a premium; the multiples here
    are the Hong Kong price's. <span class="tag">wvr</span> marks weighted
    voting rights: the founders' shares carry the vote.</p>
    <p><b>Closely held</b> (<span class="tag">close</span>) means insiders and
    strategic holders - a family, a parent - own a majority between them, by
    Yahoo's count. It says the free float is the minority, not who controls,
    and for an H-share it counts only the H tranche, so a state holding of
    A-shares does not show.</p>
    <p><b>Most of the market reports in another currency.</b> Tencent, ICBC
    and PetroChina file in renminbi, HSBC and AIA in US dollars, and all quote
    in Hong Kong dollars. Yahoo divides a Hong Kong dollar enterprise value by
    a reporting-currency EBITDA, so EV/EBITDA is repaired on every one of
    them; EPS and book value are tested per company and repaired or refused.
    The row is tagged <span class="tag">fx</span> - hover it for what was
    done.</p>
    <p><b>A <span class="tag">ttm vs fy</span> tag means read the P/E twice.</b>
    The P/E and ROE columns are Yahoo's trailing twelve months. A tagged name
    was profitable on that basis but lost money in its last filed year.</p>'''),

    # ---- JS: history reads same-basis values -------------------------------
    ('  const cur = {per: r.trailing_pe, pbr: r.price_to_book, evx: r.evx_now};',
     '  const cur = {per: r.per_now, pbr: r.pbr_now, evx: r.evx_now};'),
    ('  const now = {per: r.trailing_pe, pbr: r.price_to_book, evx: r.evx_now};',
     '  const now = {per: r.per_now, pbr: r.pbr_now, evx: r.evx_now};'),
    ('''  const passTag = r.ev.hist ? ' <span class="tag">5y low</span>' : "";''',
     '''  const passTag = r.ev.hist ? ' <span class="tag">hist low</span>' : "";'''),
    # Yahoo has four filed years for Hong Kong names, so the fifth history slot is
    # null with no year label. Show only the years that exist.
    ('    const ser = (r.h_ser && r.h_ser[k] || []).map((v, i) =>',
     '    const ser = (r.h_ser && r.h_ser[k] || []).slice(0, yrs.length).map((v, i) =>'),
    ('  // Own five-year history. Mirrors korea_filters.apply_history_screen: the',
     '  // Own filed history. Mirrors hk_filters.apply_history_screen: the'),
    ('/* Today\'s value against the five-year median, per metric.',
     '/* Today\'s value against the filed-year median, per metric.'),
    ('/* Average discount to the company\'s own five-year median, across every metric',
     '/* Average discount to the company\'s own filed-year median, across every metric'),

    # ---- JS: growth tooltip names the reporting currency -------------------
    ('  const tip = GROWTH_LABEL[key] + " (억원)\\\\n"',
     '  const tip = GROWTH_LABEL[key] + " (" + (r.fin_ccy || "reporting ccy") + " m)\\\\n"'),

    # ---- JS: symbol, tags, and the guard against one bad key ---------------
    ('''function cell(r, k) {
  const v = r[k];
  if (k === "ticker") return `<span style="color:var(--ink-3)">${esc(v)}</span>`;''',
     '''function cell(r, k) {
  const v = r[k];
  /* A key present in TABLE_COLS but absent from the row payload arrives as
     undefined, which slipped past the null guard and hit .toFixed() in the UK
     build - one renamed column left the whole table empty. Missing reads as
     missing. Computed columns read other fields and are exempt. */
  if (v === undefined && !(k in GROWTH) && k !== "hist_avg_disc"
      && k !== "screen" && k !== "name") return '<span class="na">—</span>';
  if (k === "symbol") return `<span style="color:var(--ink-3)">${esc(v)}</span>`;'''),
    ('''    const tags = (r.holdco ? '<span class="tag">holdco</span>' : "")''',
     '''    const tags = (r.ctrl ? `<span class="tag" title="Closely held: insiders and strategic holders own ${esc(r.ctrl)} (Yahoo)">close</span>` : "")
               + (r.pyr ? `<span class="tag" title="Consolidated by a listed parent: ${esc(r.pyr)}">listed parent</span>` : "")
               + (r.hsh ? '<span class="tag" title="H-share: the Hong Kong tranche of a mainland-incorporated company">h</span>' : "")
               + (r.wvr ? '<span class="tag" title="Weighted voting rights: the founders hold shares carrying the vote">wvr</span>' : "")
               + (r.fxn ? `<span class="tag" title="Reports in ${esc(r.fin_ccy)}, quotes in HKD: ${esc(r.fxn)}">fx</span>` : "")
               + (r.ttmx ? '<span class="tag" title="Profitable over the trailing twelve months, loss-making in the last filed year: the trailing P/E and ROE likely carry one-offs">ttm vs fy</span>' : "")
               + (r.holdco ? `<span class="tag" title="${esc(r.hold || 'Holding company')}">holdco</span>` : "")'''),
    ('r.name.toLowerCase().includes(q) || r.ticker.includes(q)',
     'r.name.toLowerCase().includes(q) || (r.symbol || "").toLowerCase().includes(q)'),

    # ---- storage key and comments ------------------------------------------
    ('const STORE = "kr-thresholds-" + (M.board || "x");',
     'const STORE = "hk-thresholds-" + (M.board || "x");'),
    ('/* One row against the current thresholds. Mirrors korea_filters.apply_roe_gate',
     '/* One row against the current thresholds. Mirrors hk_filters.apply_roe_gate'),
    ('''   Only serve.py can actually re-run the screen: it shells out to main_kr.py,
   which scrapes KIND and Naver. A page opened straight off disk, or published''',
     '''   Only serve.py can actually re-run the screen: it shells out to main_hk.py,
   which reads HKEX's list and Yahoo. A page opened straight off disk, or
   published'''),
]

# Labels that appear more than once, replaced everywhere.
ALL = [
    ('const lab = {per: "PER", pbr: "PBR", evx: "EV/EBITDA"};',
     'const lab = {per: "P/E", pbr: "P/B", evx: "EV/EBITDA"};'),
]

missed = []
for old, new in PAIRS:
    if old in s:
        s = s.replace(old, new, 1)
    else:
        missed.append(old[:72])
for old, new in ALL:
    n = s.count(old)
    if not n:
        missed.append(old[:72])
    s = s.replace(old, new)

io.open(P, "w", encoding="utf-8").write(s)
print(f"applied {len(PAIRS) + len(ALL) - len(missed)}/{len(PAIRS) + len(ALL)}")
for m in missed:
    print("  MISS:", m)
sys.exit(1 if missed else 0)
