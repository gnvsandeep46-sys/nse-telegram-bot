"""Pre-Market Economic Snapshot - 8:30 AM IST, once per trading day.

Open-source, keyless data only:
- Yahoo Finance chart API: US indices, US futures, DXY, USD/INR, US 10Y,
  WTI/Brent/gold/silver/copper/gas, CBOE VIX, Sensex.
- frankfurter.app (ECB reference) fallback for USD/INR.
- FRED fredgraph.csv for US 2Y (fallback: Data unavailable).
- NSE MCP live feed: Nifty/Bank/VIX previous session levels, sectoral movers.
- ForexFactory weekly JSON (nfs.faireconomy.media) for the econ calendar,
  with embedded announced FOMC/RBI MPC dates as fallback.
- Investing.com RSS (commodities / economic indicators / stock markets) for
  last-24h news, mapped to India sector impact by deterministic rules.
- PCR: NSE option-chain API attempted (usually blocked from server IPs);
  otherwise "Data unavailable" - never estimated.

Every number carries change vs previous close and the source name. Any value
that cannot be verified is shown as "Data unavailable".
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re

import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
STATE_FILE = os.path.join("state", "premarket.json")

YAHOO = {  # symbol -> (label, kind)  kind: idx|fut|rate|fx
    "^GSPC": ("S&P 500", "idx"), "^IXIC": ("Nasdaq", "idx"), "^DJI": ("Dow", "idx"),
    "ES=F": ("S&P fut", "fut"), "NQ=F": ("Nasdaq fut", "fut"), "YM=F": ("Dow fut", "fut"),
    "DX-Y.NYB": ("DXY", "idx"), "USDINR=X": ("USD/INR", "fx"),
    "^TNX": ("US 10Y yield", "rate"), "CL=F": ("WTI crude", "idx"),
    "BZ=F": ("Brent", "idx"), "GC=F": ("Gold", "idx"), "SI=F": ("Silver", "idx"),
    "HG=F": ("Copper", "idx"), "NG=F": ("NatGas", "idx"), "^VIX": ("CBOE VIX", "idx"),
    "^BSESN": ("Sensex", "idx"),
}

# Announced schedules (verified 23-Nov-2026: federalreserve.gov FOMC tentative
# schedule; RBI annualpolicy MPC schedule FY2026-27). Decision day = day 2.
FOMC_2026 = [("2026-01-27", "2026-01-28"), ("2026-03-17", "2026-03-18"),
             ("2026-04-28", "2026-04-29"), ("2026-06-16", "2026-06-17"),
             ("2026-07-28", "2026-07-29"), ("2026-09-15", "2026-09-16"),
             ("2026-10-27", "2026-10-28"), ("2026-12-08", "2026-12-09")]
RBI_MPC = [("2026-12-02", "2026-12-04"), ("2027-02-03", "2027-02-05")]

NEWS_FEEDS = [
    ("https://www.investing.com/rss/news_11.rss", "Investing.com Commodities"),
    ("https://www.investing.com/rss/news_95.rss", "Investing.com Econ Indicators"),
    ("https://www.investing.com/rss/news_25.rss", "Investing.com Stock Markets"),
]

NEWS_RULES = [
    (r"crude|\bwti\b|brent|\bopec\b|\boil\b", "crude",
     ("NEG", "OMCs (IOC/BPCL/HINDPETRO), paints (ASIANPAINT), aviation (INDIGO)",
      "crude up = higher import bill & input costs"),
     ("BEN", "OMCs, paints, aviation; ONGC/OIL benefit", "crude down = lower import bill")),
    (r"gold|silver", "precious metals",
     ("BEN", "gold loan NBFCs (MUTHOOTFIN/MANAPPURAM)", "higher metal prices"),
     ("NEU", "-", "-")),
    (r"copper", "copper", ("BEN", "HINDALCO/VEDL", "higher realisations"),
     ("NEG", "HINDALCO/VEDL", "lower realisations")),
    (r"steel|\bcoal\b", "steel/coal",
     ("BEN", "TATASTEEL/JSWSTEEL/SAIL; Coal India", "higher prices"),
     ("NEG", "TATASTEEL/JSWSTEEL/SAIL; Coal India", "lower prices")),
    (r"natural gas|\blng\b", "natgas",
     ("NEG", "GAIL, city gas (GUJGASLTD/IGL)", "higher gas input costs"),
     ("BEN", "GAIL, city gas, ONGC", "lower gas costs")),
    (r"edible oil|palm oil|soybean", "edible oil",
     ("NEG", "edible oil refiners (ADANIWILMAR/MARICO)", "higher input costs"),
     ("BEN", "edible oil refiners", "lower input costs")),
    (r"rare earth|export ban|export restriction|graphite|antimony|export curbs",
     "supply curb",
     ("NEG", "electronics (DIXON), autos/EV chain", "supply disruption risk"),
     ("NEU", "-", "-")),
    (r"tariff|sanction|trade war|import duty|trade restriction", "trade barrier",
     ("NEG", "exporters: IT, pharma, textiles, gems", "market access risk"),
     ("NEU", "-", "-")),
    (r"trade deal|trade agreement|tariff cut|trade pact", "trade deal",
     ("BEN", "exporters: IT, pharma, textiles, gems", "improved market access"),
     ("NEU", "-", "-")),
]
UP_W = re.compile(r"rise|jump|surge|climb|gain|soar|higher|up \d|spike|rally|rebound")
DN_W = re.compile(r"fall|drop|slump|slide|plunge|sink|tumble|lower|down \d|decline|"
                  r"weaken|slips?|losses|eases?")


def _esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def _get(url: str, timeout: int = 12) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA,
                                       "Accept": "*/*"}, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except requests.RequestException:
        pass
    return None


def _arrow(x: float) -> str:
    return "\u25b2" if x > 0 else ("\u25bc" if x < 0 else "\u25ac")


def _tag(tag: str) -> str:
    return {"BEN": "\U0001F7E2 BENEFICIAL", "NEG": "\U0001F534 NEGATIVE",
            "NEU": "\U0001F7E1 NEUTRAL"}.get(tag, tag)


# --------------------------------------------------------------------------- #
# yahoo levels
# --------------------------------------------------------------------------- #

def yahoo_row(sym: str) -> dict | None:
    txt = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{requests.utils.quote(sym)}?range=5d&interval=1d")
    if not txt:
        return None
    try:
        j = json.loads(txt)
        res = j["chart"]["result"][0]
        meta = res["meta"]
        closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0]
                              .get("close") or []) if c]
        last_close = closes[-1] if closes else None
        prev_close = closes[-2] if len(closes) >= 2 else None
        live = meta.get("regularMarketPrice")
        return {"sym": sym, "live": live, "last_close": last_close,
                "prev_close": prev_close,
                "chart_prev": meta.get("chartPreviousClose"),
                "ts": meta.get("regularMarketTime")}
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _chg(row: dict) -> tuple[float, float, float] | None:
    """Return (ref_price, abs_chg, pct) vs previous close."""
    if not row:
        return None
    prev = row["prev_close"] or row["chart_prev"]
    ref = row["live"] or row["last_close"]
    if ref is None or prev is None or prev == 0:
        return None
    d = ref - prev
    return ref, d, d / prev * 100.0


def us_levels_block(y: dict) -> list[str]:
    lines = ["<b>US MARKET SNAPSHOT</b>",
             "<pre>" + f"{'Item':<15.15}{'Level':>11.11}{'Chg':>10.10}{'Chg%':>8.8}   Src"]
    for sym, (label, kind) in YAHOO.items():
        if kind == "fx" or sym == "^BSESN":
            continue  # rendered in their own sections
        row = y.get(sym)
        c = _chg(row)
        if c is None:
            lines.append(f"{label:<15.15} Data unavailable")
            continue
        ref, d, p = c
        if sym == "^TNX":
            ref_s, d_s, p_s = f"{ref:.3f}%", f"{d * 100:+.0f}bps", ""
        else:
            ref_s, d_s, p_s = f"{ref:,.2f}", f"{d:+,.2f}", f"{p:+.2f}%"
        lines.append(f"{label:<15.15}{ref_s:>11.11}{d_s:>10.10}{p_s:>8.8}  Yahoo")
    lines.append("</pre>")
    lines.append("US futures are last quotes vs prior settlement (Yahoo).")
    return lines


def fx_block(y: dict) -> str:
    row = y.get("USDINR=X")
    c = _chg(row)
    if c:
        ref, d, p = c
        out = (f"USD/INR {ref:.3f} | {_arrow(d)} {d * 100:+.0f}p ({p:+.2f}%) "
               "| Yahoo")
        dc = _chg(y.get("DX-Y.NYB"))
        if dc:
            out += f"\nDXY {dc[0]:.2f} ({dc[2]:+.2f}%) | Yahoo"
        return out
    txt = _get("https://api.frankfurter.app/latest?from=USD&to=INR")
    if txt:
        try:
            v = json.loads(txt)["rates"]["INR"]
            return f"USD/INR {v:.3f} (ECB ref, frankfurter.app; chg unavailable)"
        except (ValueError, KeyError):
            pass
    return "USD/INR: Data unavailable"


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #

def _ff_events(today: dt.datetime) -> list[dict] | None:
    txt = _get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    if not txt:
        return None
    try:
        evs = json.loads(txt)
    except ValueError:
        return None
    out = []
    for e in evs:
        try:
            t = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            t_ist = t.astimezone(IST)
        except (ValueError, KeyError, TypeError):
            continue
        if e.get("country") in ("USD", "INR"):
            out.append({"title": e.get("title", ""), "ist": t_ist,
                        "impact": e.get("impact", ""), "prev": e.get("previous", ""),
                        "fc": e.get("forecast", ""), "ccy": e.get("country")})
    return out


def calendar_block(today: dt.datetime, ff: list[dict] | None) -> list[str]:
    lines = ["<b>ECONOMIC CALENDAR (next 3 days, IST)</b>"]
    end = (today + dt.timedelta(days=3)).date()
    if ff:
        sel = sorted([e for e in ff if today.date() <= e["ist"].date() <= end],
                     key=lambda e: e["ist"])[:10]
        if sel:
            lines.append("<pre>Date      Time  CCY Impact  Event (prev -> fc)")
            for e in sel:
                rel = "released" if e["ist"] < today else ""
                lines.append(f"{e['ist']:%d-%b %H:%M} {e['ccy']:<3.3} "
                             f"{e['impact'][:3]:<3.3} {_esc(e['title'][:34]):<34.34}"
                             f" p:{_esc(str(e['prev'])[:8])} f:{_esc(str(e['fc'])[:8])}"
                             f" {rel}")
            lines.append("</pre>")
            lines.append("Hot/In-line/Cool tagging needs the actual print; the "
                         "feed carries prev/forecast only. Released events: "
                         "check the actual vs forecast on the source page.")
        else:
            lines.append("No high-impact USD/INR events in feed window.")
    else:
        lines.append("Live calendar feed unavailable - announced schedules:")
        lines.append("FOMC 2026 (decision day 2): " +
                     ", ".join(f"{a[5:7]}/{a[8:10]}" for a, _b in FOMC_2026))
        lines.append("RBI MPC: Dec 2-4 2026 (decision Dec 4), Feb 3-5 2027")
        lines.append("US CPI/NFP/claims dates: Data unavailable (see BLS schedule)")
    nxt_fomc = next((b for a, b in FOMC_2026 if b >= f"{today:%Y-%m-%d}"), None)
    nxt_mpc = next((b for a, b in RBI_MPC if b >= f"{today:%Y-%m-%d}"), None)
    lines.append(f"Next FOMC: {nxt_fomc or '-'} | Next RBI MPC: {nxt_mpc or '-'}")
    return lines


def fed_watch_block(y: dict) -> str:
    # Market-implied read from 30-day Fed funds futures is not fetchable
    # keylessly and reliably; do not guess. Report schedule + note instead.
    return ("Fed watch: current range and CME FedWatch probability cannot be "
            "verified from keyless public endpoints from this server - "
            "Data unavailable. Next FOMC Dec 8-9, 2026 (decision Dec 9).")


# --------------------------------------------------------------------------- #
# india block (MCP)
# --------------------------------------------------------------------------- #

def india_block(imap: dict, y: dict) -> list[str]:
    lines = ["<b>INDIA MARKET SNAPSHOT</b> (prev session)",
             "<pre>" + f"{'Index':<12.12}{'Close':>10.10}{'Chg%':>8.8}   Src"]
    for name, disp in [("NIFTY 50", "Nifty 50"), ("NIFTY BANK", "Bank Nifty"),
                       ("INDIA VIX", "India VIX")]:
        row = imap.get(name) or {}
        last = row.get("last") or row.get("lastTradedPrice")
        pct = row.get("perChange")
        if last is None:
            lines.append(f"{disp:<12.12} Data unavailable")
        else:
            lines.append(f"{disp:<12.12}{float(last):>10,.2f}"
                         f"{(pct if pct is not None else 0):>+7.2f}%   NSE MCP")
    ses = _chg(y.get("^BSESN"))
    if ses:
        lines.append(f"{'Sensex':<12.12}{ses[0]:>10,.2f}{ses[2]:>+7.2f}%   Yahoo")
    else:
        lines.append(f"{'Sensex':<12.12} Data unavailable")
    lines.append("</pre>")
    lines.append("GIFT Nifty: Data unavailable (no keyless server-side source)")
    lines.append("India 10Y G-Sec: Data unavailable (no keyless server-side source)")
    lines.append("FII/DII net flows: Data unavailable (public sources block "
                 "server IPs; not estimated)")
    return lines


def pcr_block() -> str:
    # attempt the NSE option chain (usually blocked from datacenter IPs)
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "*/*",
                          "Accept-Language": "en-US,en;q=0.9"})
        s.get("https://www.nseindia.com", timeout=8)
        r = s.get("https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
                  timeout=10)
        if r.status_code == 200:
            j = r.json()
            data = j.get("records", {}).get("data", [])
            put_oi = sum(d.get("putOTotalContracts", 0) or 0 for d in data)
            call_oi = sum(d.get("callOTotalContracts", 0) or 0 for d in data)
            if call_oi > 0:
                pcr = put_oi / call_oi
                read = ("PCR > 1 = put OI heavier (support-heavy / hedged book)"
                        if pcr > 1 else "PCR < 1 = call OI heavier (resistance-heavy)")
                return (f"Nifty PCR {pcr:.2f} (prev-day OI, NSE option chain) "
                        f"| read: {read}")
    except (requests.RequestException, ValueError, KeyError):
        pass
    return ("Nifty PCR: Data unavailable - option chain not exposed by the "
            "NSE MCP and nseindia.com blocks server IPs (no guessing).")


# --------------------------------------------------------------------------- #
# news
# --------------------------------------------------------------------------- #

def _rss_items(feed_url: str, hours: int = 24) -> list[tuple[str, str]]:
    txt = _get(feed_url, timeout=10)
    if not txt:
        return []
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    for m in re.finditer(r"<item>(.*?)</item>", txt, re.S):
        blk = m.group(1)
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", blk, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", blk)
        if not title or not pub:
            continue
        t = html.unescape(title.group(1)).strip()
        try:
            pt = dt.datetime.strptime(pub.group(1).strip(),
                                      "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if (now - pt).total_seconds() <= hours * 3600 and t:
            out.append((t[:110], pt))
    return out


def news_block() -> list[str]:
    items: list[tuple[str, str]] = []
    for url, src in NEWS_FEEDS:
        for t, pt in _rss_items(url)[:14]:
            items.append((f"{t} [{src}]", pt))
    items.sort(key=lambda x: -x[1].timestamp())
    lines = ["<b>GLOBAL / COMMODITY NEWS (24h) - India impact</b>"]
    n = 0
    for t, _pt in items:
        low = t.lower()
        for pat, name, up, dn in NEWS_RULES:
            if re.search(pat, low):
                up_hit, dn_hit = bool(UP_W.search(low)), bool(DN_W.search(low))
                direction = ("up" if up_hit and not dn_hit else
                             "down" if dn_hit and not up_hit else None)
                if direction == "up":
                    tag, sec, why = up
                elif direction == "down":
                    tag, sec, why = dn
                else:
                    tag, sec, why = "NEU", "sector direction unclear from headline", "-"
                lines.append(f"\u2022 {_esc(t)}")
                lines.append(f"  {_tag(tag)} | {name} | {sec}")
                lines.append(f"  why: {why}")
                n += 1
                break
        if n >= 5:
            break
    if n == 0:
        lines.append("No matching commodity/trade headlines in last 24h feeds.")
    return lines


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #

def summary_block(y: dict, imap: dict) -> list[str]:
    spx = _chg(y.get("^GSPC"))
    fut = _chg(y.get("ES=F"))
    vix = imap.get("INDIA VIX") or {}
    vix_pct = vix.get("perChange")
    fx = _chg(y.get("USDINR=X"))
    wti = _chg(y.get("CL=F"))
    score, reasons, risks = 0.0, [], []
    if spx:
        s = 1.0 if spx[1] > 0 else -1.0 if spx[1] < 0 else 0
        score += s * 2
        reasons.append((f"US: S&P {spx[2]:+.2f}%", abs(s)))
        risks.append((f"S&P {spx[2]:+.2f}% overnight", abs(spx[2])))
    if fut:
        s = 1.0 if fut[1] > 0 else -1.0
        score += s
        reasons.append((f"US fut {fut[2]:+.2f}%", abs(s)))
    if vix_pct is not None:
        s = -1.0 if vix_pct > 0 else 1.0
        score += s
        reasons.append((f"India VIX {vix_pct:+.2f}%", abs(s)))
        risks.append((f"India VIX {vix_pct:+.2f}%", abs(vix_pct)))
    if fx:
        s = -1.0 if fx[1] > 0 else 1.0
        score += s * 0.5
        reasons.append((f"USDINR {fx[2]:+.2f}%", abs(s)))
    if wti:
        s = -0.5 if wti[1] > 0 else 0.5
        score += s
        risks.append((f"WTI {wti[2]:+.2f}%", abs(wti[2])))
    bias = ("Bullish \U0001F7E2" if score >= 1.5 else
            "Bearish \U0001F534" if score <= -1.5 else "Neutral \U0001F7E1")
    reasons.sort(key=lambda x: -x[1])
    risks.sort(key=lambda x: -x[1])

    nifty = imap.get("NIFTY 50") or {}
    hi, lo, cl = nifty.get("highPrice"), nifty.get("lowPrice"), nifty.get("last")
    sect_up, sect_dn = [], []
    rows = (imap.get("_sectoral") or [])
    for r in rows:
        p = r.get("perChange")
        if p is None or not r.get("indexName"):
            continue
        (sect_up if p > 0 else sect_dn).append((r["indexName"], p))
    sect_up.sort(key=lambda x: -x[1])
    sect_dn.sort(key=lambda x: x[1])

    lines = ["<b>ONE-GLANCE SUMMARY</b>"]
    lines.append(f"Today's Nifty bias: {bias} (rule-based score {score:+.1f}: "
                 "US close x2, US futures, VIX, FX, crude)")
    for i, (r, _w) in enumerate(reasons[:3], 1):
        lines.append(f"  {i}. {r}")
    lines.append("Top risks: " + "; ".join(r for r, _w in risks[:3]))
    if sect_up:
        lines.append("Sectors to watch (prev session): \u25b2 " +
                     ", ".join(f"{n.split('NIFTY ')[-1]} {p:+.1f}%" for n, p in sect_up[:3]))
    if sect_dn:
        lines.append("  \u25bc " +
                     ", ".join(f"{n.split('NIFTY ')[-1]} {p:+.1f}%" for n, p in sect_dn[:3]))
    if cl:
        lines.append(f"Nifty levels: close {float(cl):,.1f} | resistance prev high "
                     f"{float(hi):,.1f} | support prev low {float(lo):,.1f}"
                     if hi and lo else f"Nifty close {float(cl):,.1f}")
    return lines


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def _load_state() -> dict:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def run_premarket(live, send, now: dt.datetime, dry_run: bool,
                  force: bool = False) -> int:
    st = _load_state()
    today = f"{now:%Y-%m-%d}"
    if st.get("last_sent") == today and not force:
        print(f"[premarket] already sent {today} - no repetition")
        return 0

    # MCP: prev-session India levels + sectoral movers
    imap: dict = {}
    try:
        r = live.call("cm_get_live_indices", group="all", nameFilter="")
        for rows in r.get("indices", {}).values():
            for row in rows:
                nm = row.get("indexName")
                if nm:
                    imap[nm] = row
    except Exception as exc:  # noqa: BLE001
        print(f"[premarket] MCP indices unavailable: {exc}")
    try:
        r2 = live.call("cm_get_live_indices", group="sectoral", nameFilter="")
        imap["_sectoral"] = (r2.get("indices") or {}).get("sectoral") or []
    except Exception:  # noqa: BLE001
        imap["_sectoral"] = []

    y: dict = {}
    for sym in YAHOO:
        row = yahoo_row(sym)
        if row:
            y[sym] = row

    ff = _ff_events(now)

    lines = [f"<b>Pre-Market Snapshot | {now:%a %d %b %Y} | 8:30 AM IST</b>", ""]
    lines += us_levels_block(y)
    lines.append("")
    lines.append("<b>FX</b>")
    lines.append(_esc(fx_block(y)))
    lines.append("")
    lines += india_block(imap, y)
    lines.append(_esc(pcr_block()))
    lines.append("")
    lines += calendar_block(now, ff)
    lines.append(_esc(fed_watch_block(y)))
    lines.append("")
    lines += news_block()
    lines.append("")
    lines += summary_block(y, imap)
    lines.append("")
    lines.append("Data compiled from public sources (Yahoo/FRED/NSE MCP/"
                 "ForexFactory/Investing.com). Not investment advice.")

    msg = "\n".join(lines)
    send(msg)

    st["last_sent"] = today
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)
    return 0
