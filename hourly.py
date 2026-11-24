"""Hourly intraday module: index breadth, India VIX tracking, F&O top movers.

Fires at 09:30, 10:30 ... 15:30 IST (hooked inside the 5-minute intraday run
when minute == 30). Each run is a fresh snapshot; run-over-run deltas (VIX,
rank changes) come from state/hourly_state.json, committed back to the repo
by the workflow after every run.

Data rules honoured:
- Live values only, from the NSE MCP endpoints. Missing symbol quotes are
  skipped and weight coverage is reported. Nothing is estimated.
- F&O movers are filtered to the embedded F&O universe (universe.py); indices
  and non-F&O stocks are excluded.
- Verdicts: Strong (wtd adv > 65%), Weak (< 35%), Mixed otherwise; DIVERGENCE
  flagged when index direction and weighted breadth disagree.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os

from indices_data import ALL_MEMBERS, BREADTH_LETTERS, BREADTH_TABLES
from universe import FNO_ALL

STATE_FILE = os.path.join("state", "hourly_state.json")
FOOTER = "Source: NSE India (MCP). Data is indicative, not investment advice."


def _esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def _load(today: str) -> dict:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                st = json.load(fh)
            if st.get("date") == today:
                return st
        except (OSError, json.JSONDecodeError):
            pass
    return {"date": today, "runs": {}, "run_no": 0, "prev_vix": None,
            "prev_vix_label": None, "prev_gainers": [], "prev_losers": []}


def _save(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _fetch_quotes(live, wanted: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for letter in sorted({s[0] for s in wanted}):
        try:
            r = live.call("cm_get_equity_stocks", limit=500, symbolFilter=letter)
        except Exception:  # noqa: BLE001
            continue
        for row in r.get("stocks", []):
            sym = row.get("symbol", "")
            if sym in wanted and row.get("series") == "EQ":
                out[sym] = row
    return out


def _fetch_indices(live) -> dict[str, dict]:
    try:
        r = live.call("cm_get_live_indices", group="all", nameFilter="")
    except Exception:  # noqa: BLE001
        return {}
    imap: dict[str, dict] = {}
    for rows in r.get("indices", {}).values():
        for row in rows:
            name = row.get("indexName")
            if name:
                imap[name] = row
    return imap


def _pct(row: dict) -> float | None:
    for key in ("perChange", "percChange", "pChange"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _prev_close(row: dict) -> float | None:
    for key in ("previousClose", "preClosePrice", "prevClose"):
        v = row.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    last = row.get("last") or row.get("lastTradedPrice")
    pct = _pct(row)
    if isinstance(last, (int, float)) and pct is not None and abs(pct) < 100:
        return float(last) / (1.0 + pct / 100.0)
    return None


def _fmt_vol(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    if v >= 1e7:
        return f"{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"{v / 1e5:.2f} L"
    if v >= 1e3:
        return f"{v / 1e3:.1f} K"
    return f"{v:.0f}"


def _arrow(x: float) -> str:
    return "\u25b2" if x > 0 else ("\u25bc" if x < 0 else "\u25ac")


def _verdict(wtd_adv: float, idx_pct: float | None) -> str:
    if wtd_adv > 65:
        v = "Strong"
    elif wtd_adv < 35:
        v = "Weak"
    else:
        v = "Mixed"
    if idx_pct is not None:
        if idx_pct > 0.10 and wtd_adv < 50:
            v += " \u26a0DIV"
        elif idx_pct < -0.10 and wtd_adv > 50:
            v += " \u26a0DIV"
    return v


def _breadth_rows(quotes: dict, imap: dict) -> list[str]:
    lines = []
    for t in BREADTH_TABLES:
        members, w = t["members"], t["members"]
        adv = dec = unch = 0
        wsum = wadv = wdec = 0.0
        contrib: list[tuple[str, float, float, float]] = []  # sym, pct, wnorm, contrib
        for sym, wv in members.items():
            q = quotes.get(sym)
            if not q:
                continue
            pct = _pct(q)
            if pct is None:
                continue  # unknown change: excluded from counts and weights
            wn = wv / sum(w.values()) * 100.0
            wsum += wn
            if pct > 0:
                adv += 1
                wadv += wn
            elif pct < 0:
                dec += 1
                wdec += wn
            else:
                unch += 1
            contrib.append((sym, pct, wn, wn * pct))
        cov = wsum / 100.0 * 100.0
        wa = wadv / wsum * 100.0 if wsum > 0 else None
        idx_row = imap.get(t["live"], {})
        lvl = idx_row.get("last") or idx_row.get("lastTradedPrice")
        ipct = _pct(idx_row)
        name = _esc(t["key"])
        if t["note"]:
            name += " *"
        if lvl is None or ipct is None:
            lines.append(f"{name:<18.18} Data unavailable (no live row)")
            continue
        ad = f"{adv}/{dec}/{unch}" if (adv + dec + unch) else "-"
        ratio = f"{adv / max(dec, 1):.2f}" if dec or adv else "-"
        was = f"{wa:.0f}" if wa is not None else "-"
        wds = f"{100 - wa:.0f}" if wa is not None else "-"
        cov_s = f" cov{cov:.0f}%" if cov < 95 and wa is not None else ""
        lines.append(
            f"{name:<20.20}{lvl:>10,.2f}{ipct:>+7.2f}{_arrow(ipct)} {ad:>9.9}"
            f"{ratio:>7.7}{was:>6.6}{wds:>6.6}{cov_s} {_verdict(wa, ipct) if wa is not None else '-'}")
        # drags / lifters
        if contrib:
            up = sorted(contrib, key=lambda c: -c[3])[:2]
            dn = sorted(contrib, key=lambda c: c[3])[:2]
            up_s = ", ".join(f"{s} {p:+.1f}%(w{wn:.0f})" for s, p, wn, _ in up if p > 0)
            dn_s = ", ".join(f"{s} {p:+.1f}%(w{wn:.0f})" for s, p, wn, _ in dn if p < 0)
            lines.append(f"  {'lift: ' + up_s if up_s else 'lift: -'}")
            lines.append(f"  {'drag: ' + dn_s if dn_s else 'drag: -'}")
    return lines


def _vix_block(imap: dict, st: dict, now: dt.datetime) -> list[str]:
    row = imap.get("INDIA VIX")
    if not row:
        return ["India VIX: Data unavailable"]
    last = row.get("last") or row.get("lastTradedPrice")
    pct = _pct(row)
    if last is None:
        return ["India VIX: Data unavailable"]
    lines = [f"VIX {float(last):.2f}", ]
    seg = f"VIX {float(last):.2f}"
    if pct is not None:
        prev = float(last) / (1.0 + pct / 100.0)
        d = float(last) - prev
        seg += f" | {_arrow(d)} {d:+.2f} vs prev close ({pct:+.2f}%)"
    if st.get("prev_vix"):
        d2 = float(last) - st["prev_vix"]
        lbl = st.get("prev_vix_label") or "prev run"
        seg += f" | {_arrow(d2)} {d2:+.2f} vs {lbl} run"
    lines[0] = seg
    if pct is not None:
        read = ("rising VIX = fear/volatility up, caution"
                if d > 0 else "falling VIX = calm, trend more reliable")
        lines.append(f"Read: {read}.")
    return lines


def _movers(quotes: dict) -> tuple[list[tuple], list[tuple]]:
    cand = []
    for sym, q in quotes.items():
        if sym not in FNO_ALL:
            continue
        pct = _pct(q)
        ltp = q.get("lastTradedPrice") or q.get("last")
        if pct is None or ltp is None:
            continue
        cand.append((sym, float(ltp), pct, q.get("volume") or 0))
    gain = sorted(cand, key=lambda c: -c[2])[:5]
    lose = sorted(cand, key=lambda c: c[2])[:5]
    return gain, lose


def _rank_col(sym: str, prev: list[str], pos: int) -> str:
    if not prev:
        return ""
    if sym not in prev:
        return "NEW"
    old = prev.index(sym) + 1
    new = pos + 1
    if old == new:
        return "same"
    return "\u25b2" if new < old else "\u25bc"


def _mover_table(rows: list[tuple], prev: list, is_gainer: bool) -> list[str]:
    lines = [f"{'#':<2.2} {'Symbol':<11.11}{'LTP':>10.10}{'Chg%':>9.9}"
             f"{'Volume':>10.10}{'Rank':>6.6}"]
    syms = []
    for i, (sym, ltp, pct, vol) in enumerate(rows, 1):
        syms.append(sym)
        rc = _rank_col(sym, prev, i - 1)
        lines.append(f"{i:<2d} {_esc(sym):<11.11}{ltp:>10,.2f}"
                     f"{pct:>+7.2f}{_arrow(pct)} {_fmt_vol(vol):>10.10}{rc:>6.6}")
    return lines, syms


def build_message(quotes: dict, imap: dict, st: dict, now: dt.datetime,
                  run_no: int) -> tuple[str, list, list]:
    now_d = f"{now:%a %d %b %Y}"
    lines = [f"<b>NSE Live Snapshot | {now_d} | {now:%H:%M} IST | Run #{run_no}</b>",
             "",
             "<b>INDEX BREADTH</b>",
             "<pre>" + f"{'Index':<20.20}{'Lvl':>10.10}{'Chg%':>8.8}{'A/D/U':>11.11}"
             + f"{'A/D':>7.7}{'WA%':>6.6}{'WD%':>6.6}  Verdict"]
    lines += _breadth_rows(quotes, imap)
    lines.append("</pre>")
    lines.append("* embedded approximate weights; cov&lt;95% shows covered weight %")
    lines.append("")

    lines.append("<b>INDIA VIX</b>")
    lines += _vix_block(imap, st, now)
    lines.append("")

    gain, lose = _movers(quotes)
    g_lines, g_syms = _mover_table(gain, st.get("prev_gainers", []), True)
    l_lines, l_syms = _mover_table(lose, st.get("prev_losers", []), False)

    lines.append("<b>F&amp;O TOP 5 GAINERS</b>")
    lines.append("<pre>" + "\n".join(g_lines) + "</pre>")
    lines.append("<b>F&amp;O TOP 5 LOSERS</b>")
    lines.append("<pre>" + "\n".join(l_lines) + "</pre>")
    if st.get("prev_gainers"):
        lines.append("Rank col: vs previous run (NEW / \u25b2 / \u25bc / same)")
    lines.append("")
    lines.append(_esc(FOOTER))
    return "\n".join(lines), g_syms, l_syms


def run_hourly(live, send, now: dt.datetime, dry_run: bool) -> int:
    today = f"{now:%Y-%m-%d}"
    st = _load(today)
    hour = now.hour
    if str(hour) in st["runs"]:
        return 0  # already sent this hour

    quotes = _fetch_quotes(live, ALL_MEMBERS | set(FNO_ALL))
    imap = _fetch_indices(live)
    vix_row = imap.get("INDIA VIX")
    if not quotes or not vix_row:
        print(f"[hourly] skip {now:%H:%M}: quotes={len(quotes)} vix={bool(vix_row)}")
        return 0

    run_no = int(st.get("run_no", 0)) + 1
    msg, g_syms, l_syms = build_message(quotes, imap, st, now, run_no)

    st["runs"][str(hour)] = run_no
    st["run_no"] = run_no
    vrow_last = vix_row.get("last") or vix_row.get("lastTradedPrice")
    if vrow_last:
        st["prev_vix"] = float(vrow_last)
        st["prev_vix_label"] = f"{now:%H:%M}"
    st["prev_gainers"] = g_syms
    st["prev_losers"] = l_syms
    _save(st)

    send(msg)
    return 0
