"""Phase A+B intraday scanner on top of the NSE MCP snapshot bot.

Data model: every 5-minute run appends one snapshot per instrument to a JSON
state file. 5-minute "candles", VWAP, the opening range and volume deltas are
built from these snapshots (endpoint sampling), so they are approximations of
true candle OHLC; messages state which modules are unavailable instead of
inventing data.

Dropped/limited modules (no data source on the two MCP endpoints):
- option chain (PCR / max pain / IV) and option ideas
- futures OI, F&O ban list, contract master (expiry / lot size)
- market depth / true order flow (a momentum proxy is used instead)
- SENSEX (BSE, not on NSE MCP)
"""
from __future__ import annotations

import json
import math
import os
import time
import traceback
import datetime as dt

from universe import (SECTOR_INDEX, SECTOR_MEMBERS, INDEX_MODELS,
                      INDEX_WEIGHTS, FNO_ALL, UNIVERSE_LETTERS)

BHAV_MCP_URL = "https://mcp.nseindia.in/bhavcopy/cm/mcp"
STATE_FILE = os.getenv("STATE_FILE", "state/state.json")

ORB_VOL_RATIO = 1.2
ORB_MAX_RISK = 0.012
MAX_NEW_SIGNALS = 5
P_THRESHOLD = 0.65
BTST_VOL_PACE = 1.3
BTST_MAX_GAIN = 0.04
MIN_WEIGHT_COVERAGE = 30.0  # % of index weight embedded to trust mover factor

FOOTER = ("<i>Educational, not investment advice. Snapshot-based candles "
          "(approx OHLC); universe/weights approximate. Option chain, F&amp;O "
          "OI, ban list and depth are not available from NSE MCP.</i>")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))):%Y-%m-%d %H:%M:%S} IST] {msg}",
          flush=True)


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def f2(x: float) -> str:
    return f"{x:+.2f}%"


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def zscore(vals: dict[str, float], key: str) -> float:
    xs = [v for k, v in vals.items() if k != key]
    xs.append(vals[key])
    if len(xs) < 3:
        return 0.0
    mean = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs))
    return 0.0 if sd < 1e-9 else (vals[key] - mean) / sd


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def load_state(today: str) -> dict:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                st = json.load(fh)
            if st.get("date") == today:
                return st
        except (OSError, json.JSONDecodeError):
            pass
    return {"date": today, "candles": {}, "sector_candles": {}, "or": {},
            "buckets": {}, "signals": [], "idx_p": {}, "idx_alert_p": {},
            "flags": {}}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- #
# data fetching
# --------------------------------------------------------------------------- #

def call_retry(client, tool: str, tries: int = 3, **args):
    delay = 2
    err = None
    for _ in range(tries):
        try:
            return client.call(tool, **args)
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"{tool} failed after {tries} tries: {err}")


def fetch_quotes(live) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for letter in UNIVERSE_LETTERS:
        r = call_retry(live, "cm_get_equity_stocks", limit=500, symbolFilter=letter)
        if len(r.get("stocks", [])) >= 500:
            log(f"letter '{letter}': 500-row cap hit - possible truncation")
        for row in r.get("stocks", []):
            sym = row.get("symbol", "")
            if sym in FNO_ALL and row.get("series") == "EQ":
                out[sym] = row
    return out


def fetch_indices(live) -> dict[str, dict]:
    r = call_retry(live, "cm_get_live_indices", group="all", nameFilter="")
    imap: dict[str, dict] = {}
    for rows in r.get("indices", {}).values():
        for row in rows:
            name = row.get("indexName")
            if name:
                imap[name] = row
    return imap


def index_names_needed() -> set[str]:
    return set(SECTOR_INDEX.values()) | {c["live"] for c in INDEX_MODELS.values()} | {"NIFTY 50", "INDIA VIX"}


# --------------------------------------------------------------------------- #
# candles / indicators (snapshot-based)
# --------------------------------------------------------------------------- #

def append_snapshot(store: dict, key: str, hhmm: str, ltp: float, vol: float = 0.0) -> None:
    series = store.setdefault(key, [])
    if series and series[-1][0] == hhmm:
        series[-1][1] = ltp
        series[-1][2] = vol
    else:
        series.append([hhmm, ltp, vol])
    if len(series) > 90:
        del series[:len(series) - 90]


def series_candles(series: list) -> list[dict]:
    out = []
    for i, (hhmm, ltp, vol) in enumerate(series):
        dvol = 0.0 if i == 0 else max(0.0, (vol or 0.0) - (series[i - 1][2] or 0.0))
        out.append({"t": hhmm, "o": ltp, "c": ltp, "h": max(ltp, series[i - 1][1]) if i else ltp,
                    "l": min(ltp, series[i - 1][1]) if i else ltp, "v": dvol})
    return out


def get_or(st: dict, key: str) -> list | None:
    """Opening range from the first three opening-window snapshots.
    Tolerates cron delay: first sample must be by 09:19, samples taken
    before 09:30 count (spec: OR = first three 5-minute candles)."""
    if key in st["or"]:
        return st["or"][key]
    series = st["candles"].get(key) or st.get("sector_candles", {}).get(key) or []
    if not series or int(series[0][0]) > 919:
        return None
    early = [row[1] for row in series if row[0] < "0930"]
    if len(early) < 3:
        return None
    pts = early[:3]
    box = [min(pts), max(pts)]
    if box[1] > box[0] * 1.0005:
        st["or"][key] = box
        return box
    return None


def snapshot_vwap(series: list) -> float | None:
    num = den = 0.0
    for i in range(1, len(series)):
        dvol = max(0.0, (series[i][2] or 0.0) - (series[i - 1][2] or 0.0))
        num += series[i][1] * dvol
        den += dvol
    return num / den if den > 0 else None


def structure_score(series: list) -> float:
    """+1 per HH/HL, -1 per LH/LL on 3-bar fractal pivots of the snapshot closes."""
    closes = [row[1] for row in series]
    if len(closes) < 5:
        return 0.0
    pivots = []
    for i in range(1, len(closes) - 1):
        if closes[i] > closes[i - 1] and closes[i] > closes[i + 1]:
            pivots.append((i, "H"))
        elif closes[i] < closes[i - 1] and closes[i] < closes[i + 1]:
            pivots.append((i, "L"))
    score = n = 0
    for kind in ("H", "L"):
        pts = [c for i, k in pivots if k == kind for c in [closes[i]]]
        for j in range(1, len(pts)):
            score += 1 if pts[j] > pts[j - 1] else -1
            n += 1
    return clamp(score / n) if n else 0.0


# --------------------------------------------------------------------------- #
# STEP 1: sector ranking
# --------------------------------------------------------------------------- #

def sector_stats(st: dict, imap: dict, quotes: dict, nifty: dict) -> dict:
    nifty_pc = nifty.get("percChange") or 0.0
    nifty_open_gain = 0.0
    if nifty.get("last") and nifty.get("open"):
        nifty_open_gain = (nifty["last"] / nifty["open"] - 1) * 100
    stats: dict[str, dict] = {}
    for disp, live_name in SECTOR_INDEX.items():
        row = imap.get(live_name)
        priced = [quotes[s] for s in SECTOR_MEMBERS[disp] if s in quotes]
        if not row or not priced:
            continue
        breadth = sum(1 for q in priced if (q.get("perChange") or 0) > 0) / len(priced)
        rel = (row.get("percChange") or 0.0) - nifty_pc
        rel_open = 0.0
        if row.get("last") and row.get("open"):
            rel_open = ((row["last"] / row["open"] - 1) * 100) - nifty_open_gain
        stats[disp] = {"row": row, "rel": rel, "rel_open": rel_open,
                       "breadth": breadth, "members": len(priced),
                       "structure": structure_score(st["sector_candles"].get(live_name, []))}
    for disp, s in stats.items():
        s["composite"] = (0.5 * zscore({d: v["rel"] for d, v in stats.items()}, disp)
                          + 0.3 * s["structure"]
                          + 0.2 * zscore({d: v["breadth"] for d, v in stats.items()}, disp))
    return stats


def update_buckets(st: dict, stats: dict) -> tuple[list, list, bool]:
    ranked = sorted(stats, key=lambda d: stats[d]["composite"], reverse=True)
    top3 = [d for d in ranked[:3] if stats[d]["composite"] > 0]
    bot3 = [d for d in ranked[-3:] if stats[d]["composite"] < 0]
    pend = st["buckets"].setdefault("pending", {})
    conf = st["buckets"].setdefault("confirmed", {})
    key = json.dumps([top3, bot3], sort_keys=True)
    changed = False
    if pend.get("sig") == key and conf.get("sig") != key:
        conf.update({"sig": key, "long": top3, "short": bot3})
        changed = True
    pend["sig"] = key
    if not conf.get("sig"):
        conf.update({"sig": key, "long": top3, "short": bot3})
        changed = True
    return conf.get("long", top3), conf.get("short", bot3), changed


# --------------------------------------------------------------------------- #
# STEP 3: index model (option-chain factor unavailable)
# --------------------------------------------------------------------------- #

def index_model(st: dict, cfg: dict, imap: dict, quotes: dict,
                longs: list, shorts: list) -> dict:
    row = imap.get(cfg["live"])
    if not row:
        return {}
    series = st["sector_candles"].get(cfg["live"], [])
    factors: list[tuple[float, float, str]] = []

    weights = INDEX_WEIGHTS.get(cfg["live"], {})
    if weights:
        contrib = covered = 0.0
        for sym, w in weights.items():
            q = quotes.get(sym)
            if q:
                contrib += w * (q.get("perChange") or 0.0) / 100.0
                covered += w
        if covered >= MIN_WEIGHT_COVERAGE:
            factors.append((0.25, clamp(contrib / 0.01),
                            f"mover contribution {contrib * 100:+.2f}pp "
                            f"(~{covered:.0f}% weight covered)"))
        else:
            factors.append((0.25, 0.0, f"mover factor skipped (only {covered:.0f}% "
                                       f"weight mapped)"))

    or_box = st["or"].get(cfg["live"])
    if or_box:
        orb = 0.0
        if series:
            orb = 1.0 if series[-1][1] > or_box[1] else (-1.0 if series[-1][1] < or_box[0] else 0.0)
        factors.append((0.20, orb, f"ORB {'up' if orb > 0 else 'down' if orb < 0 else 'inside'} "
                                   f"({or_box[0]:.0f}-{or_box[1]:.0f})"))

    if len(series) >= 3:
        mom = (series[-1][1] - series[-2][1]) / max(1e-9, 0.0015 * series[-2][1])
        factors.append((0.20, clamp(mom), "flow proxy: 5-min momentum "
                                          "(no OI/depth/tick data on MCP)"))
    else:
        factors.append((0.20, 0.0, "flow proxy neutral (warming up)"))

    vwap = snapshot_vwap(series)
    pc = 0.5 * (1.0 if vwap and row["last"] > vwap else -1.0 if vwap else 0.0)
    pdh, pdl = row.get("high"), row.get("low")
    rng = clamp((row["last"] - pdl) / (pdh - pdl) * 2 - 1) if pdh and pdl and pdh > pdl else 0.0
    pc += 0.5 * rng
    factors.append((0.10, clamp(pc), f"vs VWAP {'above' if vwap and row['last'] > vwap else 'below'}; "
                                     f"day-range pos {rng:+.2f}"))

    sec = cfg.get("sector")
    if sec:
        al = 1.0 if sec in longs else (-1.0 if sec in shorts else 0.0)
        factors.append((0.05, al, f"sector {sec} bucket {al:+.0f}"))
    else:
        al = (len(longs) - len(shorts)) / 3.0
        factors.append((0.05, clamp(al), f"sector skew {al:+.2f}"))

    wsum = sum(w for w, _, _ in factors)
    s_norm = sum(w * v for w, v, _ in factors) / wsum
    p_long = 1 / (1 + math.exp(-3 * s_norm))
    return {"row": row, "p": p_long, "s": s_norm, "wsum": wsum,
            "factors": factors, "or": or_box, "vwap": vwap, "series": series}


# --------------------------------------------------------------------------- #
# STEP 2: ORB stock signals
# --------------------------------------------------------------------------- #

def orb_signals(st: dict, quotes: dict, stats: dict, longs: list, shorts: list,
                now: dt.datetime) -> list[dict]:
    if not st["or"].get("_scan_ready"):
        return []
    cand: list[dict] = []
    for direction, buckets in (("LONG", longs), ("SHORT", shorts)):
        for disp in buckets:
            for sym in SECTOR_MEMBERS[disp]:
                q = quotes.get(sym)
                series = st["candles"].get(sym) or []
                or_box = st["or"].get(sym)
                if not q or len(series) < 4 or not or_box:
                    continue
                ltp = q["lastTradedPrice"]
                vols_all = [c["v"] for c in series_candles(series)[1:]]
                if not vols_all or vols_all[-1] <= 0:
                    continue  # no fresh volume this bar -> breakout not active now
                prior = [v for v in vols_all[:-1] if v > 0][-10:]
                avg_vol = sum(prior) / len(prior) if prior else 0.0
                vol_ratio = (vols_all[-1] / avg_vol) if avg_vol > 0 else 0.0
                vwap = snapshot_vwap(series)
                orl, orh = or_box
                if direction == "LONG":
                    broke = series[-1][1] > orh
                    ok_vwap = vwap is not None and ltp > vwap
                    ok_rel = (q.get("perChange") or 0) > (stats[disp]["row"].get("percChange") or 0)
                    ok_entry = ltp <= orh + 0.5 * (orh - orl)
                else:
                    broke = series[-1][1] < orl
                    ok_vwap = vwap is not None and ltp < vwap
                    ok_rel = (q.get("perChange") or 0) < (stats[disp]["row"].get("percChange") or 0)
                    ok_entry = ltp >= orl - 0.5 * (orh - orl)
                if not broke or vol_ratio < ORB_VOL_RATIO or not ok_vwap or not ok_rel or not ok_entry:
                    continue
                entry, sl = ltp, (orl + orh) / 2
                risk = abs(entry - sl) / entry
                if risk > ORB_MAX_RISK or risk < 1e-6:
                    continue
                r_unit = abs(entry - sl)
                t1 = entry + r_unit if direction == "LONG" else entry - r_unit
                t2 = entry + 2 * r_unit if direction == "LONG" else entry - 2 * r_unit
                cand.append({"symbol": sym, "dir": direction, "sector": disp,
                             "rank": (longs.index(disp) + 1) if disp in longs else (shorts.index(disp) + 1),
                             "entry": entry, "sl": sl, "t1": t1, "t2": t2,
                             "vol": vol_ratio,
                             "score": stats[disp]["composite"] * vol_ratio})
    cand.sort(key=lambda c: c["score"], reverse=True)
    seen = {s["key"] for s in st["signals"]}
    out = []
    for c in cand:
        if len(out) >= MAX_NEW_SIGNALS:
            break
        setup = "ORB_L" if c["dir"] == "LONG" else "ORB_S"
        key = f"{now:%Y-%m-%d}|{c['symbol']}|{c['dir']}|{setup}"
        if key in seen:
            continue
        c["key"], c["setup"] = key, setup
        out.append(c)
        seen.add(key)  # one F&O name can belong to 2 sector buckets
    return out


# --------------------------------------------------------------------------- #
# STEP 4: BTST (15:15)
# --------------------------------------------------------------------------- #

def btst_scan(st: dict, bhav, quotes: dict, stats: dict, longs: list, shorts: list,
              models: dict, now: dt.datetime) -> str:
    flags = st["flags"]
    if flags.get("btst_sent"):
        return ""
    if now.time() < dt.time(15, 15):
        return ""
    if not st["or"].get("_scan_ready"):
        return (f"<b>BTST {now:%H:%M} IST</b>: skipped today - opening range not "
                f"formed (scan started after 09:30).\n{FOOTER}")
    today = f"{now:%Y-%m-%d}"
    tomorrow = (now + dt.timedelta(days=1)).date().isoformat()
    horizon = (now + dt.timedelta(days=5)).date().isoformat()

    def corp_ok(sym: str) -> bool:
        try:
            evs = call_retry(bhav, "get_corporate_actions", symbol=sym,
                             fromDate=tomorrow, toDate=horizon)
            if isinstance(evs, dict) and (evs.get("count") or 0) > 0:
                return False
            return True
        except Exception:  # noqa: BLE001
            return True  # cannot verify -> keep candidate, message notes it

    def vol_ok(sym: str) -> tuple[bool, float]:
        try:
            r = call_retry(bhav, "get_volume_analysis", symbol=sym, days=10)
            avg = r.get("avgVolume") or r.get("average_volume") or r.get("avg_volume")
            if not avg:
                return True, 0.0
            pace = (quotes[sym].get("volume") or 0) / float(avg)
            return pace >= BTST_VOL_PACE, pace
        except Exception:  # noqa: BLE001
            return True, 0.0  # cannot verify -> keep candidate

    cands = []
    seen_bt: set[str] = set()
    for direction, buckets in (("LONG", longs), ("SHORT", shorts)):
        for disp in buckets:
            for sym in SECTOR_MEMBERS[disp]:
                if sym in seen_bt:
                    continue
                q = quotes.get(sym)
                series = st["candles"].get(sym) or []
                or_box = st["or"].get(sym)
                if not q or not or_box or len(series) < 3:
                    continue
                ltp = q["lastTradedPrice"]
                hi, lo = q.get("highPrice"), q.get("lowPrice")
                if not hi or not lo or hi <= lo:
                    continue
                pos = (ltp - lo) / (hi - lo)
                vwap = snapshot_vwap(series)
                pc = q.get("perChange") or 0.0
                orl, orh = or_box
                if direction == "LONG":
                    ok = (pos >= 0.75 and vwap is not None and ltp > vwap
                          and ltp > orh and pc < BTST_MAX_GAIN * 100)
                else:
                    ok = (pos <= 0.25 and vwap is not None and ltp < vwap
                          and ltp < orl and pc > -BTST_MAX_GAIN * 100)
                if not ok:
                    continue
                pace_ok, pace = vol_ok(sym)
                if not pace_ok:
                    continue
                if not corp_ok(sym):
                    continue
                seen_bt.add(sym)
                cands.append({"symbol": sym, "dir": direction, "sector": disp,
                              "entry": ltp, "sl": lo if direction == "LONG" else hi,
                              "tgt": (ltp + (ltp - lo)) if direction == "LONG"
                              else (ltp - (hi - ltp)),  # 1R, not day high/low
                              "pace": pace, "pos": pos})
    cands.sort(key=lambda c: -c["pace"])
    lines = [f"<b>BTST report {now:%H:%M} IST</b>"]
    for c in cands[:3]:
        lines.append(f"[BTST {c['dir']}] {esc(c['symbol'])} | {esc(c['sector'])} | "
                     f"entry ~{c['entry']:.2f} | SL {c['sl']:.2f} | target {c['tgt']:.2f} | "
                     f"vol pace {c['pace']:.1f}x | day-pos {c['pos'] * 100:.0f}%")
    lines.append("")
    for key, m in models.items():
        if not m:
            continue
        bias = "LONG" if m["p"] >= P_THRESHOLD else ("SHORT" if m["p"] <= 1 - P_THRESHOLD else "NEUTRAL")
        lines.append(f"{esc(key)}: {bias} (model {m['p'] * 100:.0f}% long)")
    if not cands:
        lines.append("No BTST setup today.")
    lines.append("")
    lines.append("Exit rule: exit 09:30-10:00 next session if price opens against "
                 "the position; no averaging. F&amp;O ban list / futures OI / "
                 "global cues not available on MCP - filters skipped.")
    lines.append(f"data: {now:%d-%b-%Y %H:%M} IST, NSE MCP")
    lines.append(FOOTER)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #

def notes_block(or_ready: bool) -> str:
    lines = ["<b>Notes</b>",
             "- option chain / futures OI / ban list / depth: no MCP tool - skipped"]
    if not or_ready:
        lines.append("- opening range not formed (scan started after 09:30) - "
                     "ORB signals + index ORB factor skipped today")
    return "\n".join(lines)


def sector_table_message(stats: dict, longs: list, shorts: list, models: dict,
                         now: dt.datetime, or_ready: bool) -> str:
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["composite"], reverse=True)
    n = len(ranked)
    lines = [f"<b>Sector ranking {now:%H:%M} IST</b> (top 3 / bottom 3)",
             "rank | sector | rel% | rel open | struct | breadth | composite"]
    ellipsis = False
    for i, (disp, s) in enumerate(ranked, 1):
        if i > 3 and i <= n - 3:
            if not ellipsis:
                lines.append("  ...")
                ellipsis = True
            continue
        tag = " LONG" if disp in longs else (" SHORT" if disp in shorts else "")
        lines.append(f"{i}. {esc(disp)}{tag} | {f2(s['rel'])} | {f2(s['rel_open'])} | "
                     f"{s['structure']:+.2f} | {s['breadth'] * 100:.0f}% | "
                     f"{s['composite']:+.2f}")
    lines.append("")
    for key, m in models.items():
        if not m:
            continue
        bias = "LONG" if m["p"] >= P_THRESHOLD else ("SHORT" if m["p"] <= 1 - P_THRESHOLD else "NEUTRAL")
        lines.append(f"{esc(key)} | bias {bias} | model {m['p'] * 100:.0f}% long "
                     f"({len(m['factors'])} factors)")
    lines.append("")
    lines.append(notes_block(or_ready))
    lines.append(f"data: {now:%d-%b-%Y %H:%M} IST, NSE MCP snapshot")
    lines.append(FOOTER)
    return "\n".join(lines)


def signals_message(new_signals: list, now: dt.datetime) -> str:
    lines = [f"<b>New signals {now:%H:%M} IST</b>"]
    for c in new_signals:
        lines.append(f"[{c['dir']}] {esc(c['symbol'])} | {esc(c['sector'])} ({c['rank']}) | "
                     f"entry {c['entry']:.2f} | SL {c['sl']:.2f} | T1 {c['t1']:.2f} | "
                     f"T2 {c['t2']:.2f} | Vol {c['vol']:.1f}x | {now:%H:%M}")
    lines.append(f"data: {now:%d-%b-%Y %H:%M} IST, NSE MCP snapshot")
    lines.append(FOOTER)
    return "\n".join(lines)


def _index_signal_message(key: str, direction: str, m: dict, now: dt.datetime,
                          flip: bool = False) -> str:
    reasons = sorted(m["factors"], key=lambda f: -abs(f[0] * f[1]))[:3]
    rtxt = "; ".join(lbl for _, _, lbl in reasons)
    entry = m["row"]["last"]
    orl, orh = m["or"] if m["or"] else (entry * 0.99, entry * 1.01)
    sl = orl if direction == "LONG" else orh
    r_unit = abs(entry - sl)
    t1 = entry + r_unit if direction == "LONG" else entry - r_unit
    t2 = entry + 2 * r_unit if direction == "LONG" else entry - 2 * r_unit
    head = "Re-send (probability flip)" if flip else "Signal"
    return (f"<b>{head}: {esc(key)} {direction}</b> | model {m['p'] * 100:.0f}% long | "
            f"entry {entry:.2f} | SL {sl:.2f} | T1 {t1:.2f} | T2 {t2:.2f}\n"
            f"Top reasons: {esc(rtxt)}\n"
            f"Option idea unavailable (no contract master/chain on MCP).\n"
            f"data: {now:%d-%b-%Y %H:%M} IST\n{FOOTER}")


# --------------------------------------------------------------------------- #
# main scan
# --------------------------------------------------------------------------- #

def run_scan(live, bhav, send, now: dt.datetime, dry_run: bool) -> int:
    today = f"{now:%Y-%m-%d}"
    st = load_state(today)
    hhmm = now.strftime("%H%M")
    try:
        quotes = fetch_quotes(live)
        imap = fetch_indices(live)
        nifty = imap.get("NIFTY 50", {})
        if not quotes or not nifty:
            raise RuntimeError("equity or index data empty")

        for sym, q in quotes.items():
            append_snapshot(st["candles"], sym, hhmm, q["lastTradedPrice"], q.get("volume") or 0.0)
        for live_name in index_names_needed():
            row = imap.get(live_name)
            if row:
                append_snapshot(st["sector_candles"], live_name, hhmm, row["last"])

        or_ready = bool(st["or"].get("_scan_ready"))
        if not or_ready:
            starts = [int(v[0][0]) for v in st["candles"].values() if v]
            early_ok = sum(1 for v in st["candles"].values()
                           if v and int(v[0][0]) <= 919
                           and sum(1 for row in v if row[0] < "0930") >= 3)
            if starts and min(starts) <= 919 and \
                    early_ok >= max(5, len(st["candles"]) // 4):
                # readiness must not hinge on one arbitrary stock's OR validity:
                # per-key ORs are set only where the box is usable.
                for key in list(st["candles"]) + list(index_names_needed()):
                    get_or(st, key)
                st["or"]["_scan_ready"] = True
                or_ready = True
                log(f"opening range locked ({early_ok} stocks with full open window)")

        stats = sector_stats(st, imap, quotes, nifty)
        longs, shorts, changed = update_buckets(st, stats)
        models = {key: index_model(st, cfg, imap, quotes, longs, shorts)
                  for key, cfg in INDEX_MODELS.items()}

        flags = st["flags"]
        if now.time() >= dt.time(9, 30) and (not flags.get("sector_table_sent") or changed):
            send(sector_table_message(stats, longs, shorts, models, now, or_ready))
            flags["sector_table_sent"] = True

        new_sig = orb_signals(st, quotes, stats, longs, shorts, now)
        if new_sig:
            mid = send(signals_message(new_sig, now))
            for c in new_sig:
                c["msg_id"] = mid if isinstance(mid, int) else None
                c["status"] = "open"
            st["signals"].extend(new_sig)

        idx_ltp = {k: m["row"]["last"] for k, m in models.items() if m}
        alert_p: dict = st.setdefault("idx_alert_p", {})
        for key, m in models.items():
            if not m or not or_ready or not m.get("or"):
                continue
            st["idx_p"][key] = round(m["p"], 3)
            ltp_now = m["series"][-1][1] if m["series"] else None
            broke_up = ltp_now is not None and ltp_now > m["or"][1]
            broke_dn = ltp_now is not None and ltp_now < m["or"][0]
            long_sig = m["p"] >= P_THRESHOLD and broke_up
            short_sig = m["p"] <= 1 - P_THRESHOLD and broke_dn
            if not (long_sig or short_sig):
                continue
            direction = "LONG" if long_sig else "SHORT"
            ikey = f"{today}|{key}|{direction}|IDX_{'L' if long_sig else 'S'}"
            existing = next((s for s in st["signals"] if s["key"] == ikey), None)
            akey = f"{key}|{direction}"
            if existing:
                # re-alert only on a >=10pp probability swing vs the last alerted level
                if abs(m["p"] - alert_p.get(akey, m["p"])) >= 0.10:
                    send(_index_signal_message(key, direction, m, now, flip=True))
                    alert_p[akey] = round(m["p"], 3)
                continue
            mid = send(_index_signal_message(key, direction, m, now))
            entry = m["row"]["last"]
            sl = m["or"][0] if long_sig else m["or"][1]
            r_unit = abs(entry - sl)
            st["signals"].append({"key": ikey, "symbol": key, "dir": direction,
                                  "setup": "IDX_L" if long_sig else "IDX_S",
                                  "entry": entry, "sl": sl,
                                  "t1": entry + r_unit if long_sig else entry - r_unit,
                                  "t2": entry + 2 * r_unit if long_sig else entry - 2 * r_unit,
                                  "msg_id": mid if isinstance(mid, int) else None,
                                  "status": "open", "p": round(m["p"], 3)})
            alert_p[akey] = round(m["p"], 3)

        for sig in st["signals"]:
            if sig["status"] not in ("open", "t1"):
                continue
            if sig["setup"].startswith("ORB"):
                q = quotes.get(sig["symbol"])
                ltp = q.get("lastTradedPrice") if q else None
            elif sig["setup"].startswith("IDX"):
                ltp = idx_ltp.get(sig["symbol"])
            else:
                continue
            if ltp is None or not sig.get("msg_id"):
                continue
            if ((sig["dir"] == "LONG" and ltp <= sig["sl"]) or
                    (sig["dir"] == "SHORT" and ltp >= sig["sl"])):
                sig["status"] = "sl"
                send(f"{esc(sig['symbol'])}: SL hit at {ltp:.2f} ({now:%H:%M})",
                     reply_to=sig["msg_id"])
            elif sig["t2"] and ((sig["dir"] == "LONG" and ltp >= sig["t2"]) or
                                (sig["dir"] == "SHORT" and ltp <= sig["t2"])):
                sig["status"] = "t2"
                send(f"{esc(sig['symbol'])}: T2 hit at {ltp:.2f} ({now:%H:%M})",
                     reply_to=sig["msg_id"])
            elif sig["status"] == "open" and sig["t1"] and (
                    (sig["dir"] == "LONG" and ltp >= sig["t1"]) or
                    (sig["dir"] == "SHORT" and ltp <= sig["t1"])):
                sig["status"] = "t1"
                send(f"{esc(sig['symbol'])}: T1 hit at {ltp:.2f} ({now:%H:%M})",
                     reply_to=sig["msg_id"])

        btst = btst_scan(st, bhav, quotes, stats, longs, shorts, models, now)
        if btst:
            send(btst)
            st["flags"]["btst_sent"] = True  # only after the send actually succeeded

        save_state(st)
        ranked = sorted(stats, key=lambda d: stats[d]["composite"], reverse=True)
        log(f"scan {hhmm}: {len(quotes)} quotes, top={ranked[:3]}, bottom={ranked[-3:]}, "
            f"signals={len(st['signals'])}, or_ready={or_ready}")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"scan failed: {exc}")
        traceback.print_exc()
        flags = st.setdefault("flags", {})
        if not flags.get("alert_sent"):
            flags["alert_sent"] = True
            send(f"NSE scanner alert {now:%H:%M} IST: scan failed "
                 f"({esc(str(exc))[:200]}). Will retry next tick.")
            save_state(st)
        return 0
