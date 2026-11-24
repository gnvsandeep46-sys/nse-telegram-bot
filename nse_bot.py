#!/usr/bin/env python3
"""
nse_bot.py — NSE market snapshots to Telegram, via the official NSE MCP servers.

Intraday mode (runs every 5 minutes, Mon-Fri 09:15-15:30 IST):
  - Index snapshot   : NIFTY 50 / NIFTY BANK / NIFTY FIN SERVICE + INDIA VIX
  - Top gainers/losers (all securities)
  - Market breadth from latest published bhavcopy (labelled with its date)
EOD mode (runs once per trading day after close):
  - Full-day digest: benchmarks, breadth, India VIX, sector movers,
    top stock gainers/losers, most active by turnover.

Data sources (public MCP servers, JSON-RPC over Streamable HTTP):
  - Live    : https://mcp.nseindia.in/cmmkt/mcp
  - Bhavcopy: https://mcp.nseindia.in/bhavcopy/cm/mcp

Usage:
  python nse_bot.py --mode intraday [--force] [--dry-run]
  python nse_bot.py --mode eod      [--force] [--dry-run]

Environment:
  TELEGRAM_BOT_TOKEN   required (BotFather token) unless --dry-run
  TELEGRAM_CHAT_ID     required (chat / group id) unless --dry-run
  TELEGRAM_THREAD_ID   optional, forum topic id inside a group
  TOP_N                stocks per gainer/loser list (default 5, max 25)
  LIVE_MCP_URL         override live MCP endpoint
  BHAV_MCP_URL         override bhavcopy MCP endpoint
  BHAVCOPY_POLL_TRIES  EOD: attempts waiting for today's bhavcopy (default 5)
  BHAVCOPY_POLL_WAIT   EOD: seconds between attempts (default 150)

Exit codes: 0 = ok or intentionally skipped, 1 = real failure.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import datetime as dt
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

IST = ZoneInfo("Asia/Kolkata")

LIVE_MCP_URL = os.getenv("LIVE_MCP_URL", "https://mcp.nseindia.in/cmmkt/mcp")
BHAV_MCP_URL = os.getenv("BHAV_MCP_URL", "https://mcp.nseindia.in/bhavcopy/cm/mcp")

TOP_N = min(int(os.getenv("TOP_N", "5") or 5), 25)
BHAVCOPY_POLL_TRIES = int(os.getenv("BHAVCOPY_POLL_TRIES", "5"))
BHAVCOPY_POLL_WAIT = int(os.getenv("BHAVCOPY_POLL_WAIT", "150"))

TELEGRAM_API = "https://api.telegram.org"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
TG_THREAD = os.getenv("TELEGRAM_THREAD_ID", "")
TG_MSG_LIMIT = 4000  # stay under Telegram's 4096-char hard limit

HTTP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": "nse-telegram-bot/1.0 (python-requests)",
}

# Intraday window (IST). Cron fires 09:15-15:30; small buffers absorb the
# documented scheduling delays of GitHub Actions cron.
INTRADAY_START = dt.time(9, 10)
INTRADAY_END = dt.time(15, 35)
EOD_AFTER = dt.time(15, 30)
EOD_STRICT_UNTIL = dt.time(17, 30)  # silent exit deadline when bhavcopy missing

# Official NSE trading holidays (Capital Market segment) — update when NSE
# publishes the next year's list: https://www.nseindia.com/resources/holiday-market
NSE_HOLIDAYS: dict[int, dict[dt.date, str]] = {
    2026: {
        dt.date(2026, 1, 15): "Municipal Corporation Election (Maharashtra)",
        dt.date(2026, 1, 26): "Republic Day",
        dt.date(2026, 3, 3): "Holi",
        dt.date(2026, 3, 26): "Shri Ram Navami",
        dt.date(2026, 3, 31): "Shri Mahavir Jayanti",
        dt.date(2026, 4, 3): "Good Friday",
        dt.date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
        dt.date(2026, 5, 1): "Maharashtra Day",
        dt.date(2026, 5, 28): "Bakri Id",
        dt.date(2026, 6, 26): "Muharram",
        dt.date(2026, 9, 14): "Ganesh Chaturthi",
        dt.date(2026, 10, 2): "Mahatma Gandhi Jayanti",
        dt.date(2026, 10, 20): "Dussehra",
        dt.date(2026, 11, 10): "Diwali - Balipratipada",
        dt.date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
        dt.date(2026, 12, 25): "Christmas",
    },
}

DISCLAIMER = "Source: NSE MCP. Educational/research use only; not investment advice."


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(IST):%Y-%m-%d %H:%M:%S} IST] {msg}", flush=True)


def ist_now() -> dt.datetime:
    return dt.datetime.now(IST)


# --------------------------------------------------------------------------- #
# Minimal MCP (Model Context Protocol) client — Streamable HTTP transport
# --------------------------------------------------------------------------- #


class MCPError(RuntimeError):
    pass


class MCPClient:
    """JSON-RPC client for one MCP server endpoint (no auth needed)."""

    def __init__(self, url: str, name: str):
        self.url = url
        self.name = name
        self.session_id: str | None = None
        self._id = 0

    # -- low level ---------------------------------------------------------- #

    def _post(self, payload: dict, timeout: int = 30) -> requests.Response:
        headers = dict(HTTP_HEADERS)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.post(self.url, headers=headers, json=payload, timeout=timeout)
                if r.status_code >= 500:
                    last_exc = MCPError(f"{self.name}: HTTP {r.status_code}")
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code in (400, 404) and self.session_id:
                    # session may have expired server-side -> re-initialize once
                    log(f"{self.name}: HTTP {r.status_code}, re-initializing session")
                    self.session_id = None
                    self.start()
                    headers = dict(HTTP_HEADERS)
                    if self.session_id:
                        headers["Mcp-Session-Id"] = self.session_id
                    r = requests.post(self.url, headers=headers, json=payload, timeout=timeout)
                return r
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 * (attempt + 1))
        raise MCPError(f"{self.name}: request failed after retries: {last_exc}")

    @staticmethod
    def _parse_body(resp: requests.Response):
        """Return decoded JSON-RPC message from a JSON or SSE response body."""
        text = resp.text or ""
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:")):
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload and payload != "[DONE]":
                        try:
                            return json.loads(payload)
                        except json.JSONDecodeError:
                            continue
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return None

    # -- protocol ----------------------------------------------------------- #

    def start(self) -> None:
        self._id = 0
        r = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "nse-telegram-bot", "version": "1.0"},
            },
        })
        # The session id comes back in the HEADERS of the initialize response.
        sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
        self.session_id = sid
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        log(f"{self.name}: session ready ({sid[:8] if sid else 'stateless'})")

    def call(self, tool: str, timeout: int = 60, **arguments):
        self._id += 1
        r = self._post({
            "jsonrpc": "2.0", "id": 1000 + self._id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }, timeout=timeout)
        body = self._parse_body(r)
        if body is None:
            raise MCPError(f"{self.name}/{tool}: unreadable response (HTTP {r.status_code})")
        if body.get("error"):
            raise MCPError(f"{self.name}/{tool}: JSON-RPC error {body['error']}")
        result = body.get("result", {})
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            raise MCPError(f"{self.name}/{tool}: tool error: {' '.join(texts)[:300]}")
        out: list = []
        for item in result.get("content", []):
            if item.get("type") != "text":
                continue
            raw = item.get("text", "")
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                out.append(raw)
        if not out:
            raise MCPError(f"{self.name}/{tool}: empty result")
        return out[0] if len(out) == 1 else out


# --------------------------------------------------------------------------- #
# Calendar / gates
# --------------------------------------------------------------------------- #


def holiday_name(day: dt.date) -> str | None:
    return NSE_HOLIDAYS.get(day.year, {}).get(day)


def in_intraday_window(now: dt.datetime) -> bool:
    return INTRADAY_START <= now.time() <= INTRADAY_END and now.weekday() < 5


def eod_allowed(now: dt.datetime) -> bool:
    return now.time() >= EOD_AFTER and now.weekday() < 5


def gate_check(mode: str, force: bool) -> str | None:
    """Return a skip-reason string, or None to proceed."""
    now = ist_now()
    today = now.date()
    if force:
        return None
    if holiday_name(today):
        return f"NSE holiday today ({holiday_name(today)}) - staying silent"
    if mode == "intraday":
        if not in_intraday_window(now):
            return f"outside intraday window ({INTRADAY_START}-{INTRADAY_END} IST)"
    elif mode == "eod":
        if not eod_allowed(now):
            return f"before EOD time ({EOD_AFTER} IST)"
    elif mode == "premarket":
        if not (dt.time(8, 10) <= now.time() <= dt.time(9, 10)):
            return "outside premarket window (08:10-09:10 IST)"
    return None


def parse_nse_ts(ts: str | None) -> dt.date | None:
    """Parse NSE timestamps like '20-Nov-2026 15:30:00' or '20-Nov-2026 15:40'."""
    if not ts:
        return None
    head = str(ts).strip().split(" ")[0]
    for fmt in ("%d-%b-%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def inr(x, dp: int = 2) -> str:
    """Indian digit grouping: 64635.2 -> 64,635.20 ; 1392 -> 1,392."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "-"
    neg = x < 0
    s = f"{abs(x):.{dp}f}"
    ip, _, fp = s.partition(".")
    if len(ip) > 3:
        head, tail = ip[:-3], ip[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        ip = ",".join(groups + [tail])
    out = ip + (("." + fp) if fp else "")
    return ("-" if neg else "") + out


def qty_str(v) -> str:
    """Volume in lakh / crore notation: 5266433 -> 52.66 L ; 7.01e8 -> 70.11 Cr."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    if abs(v) >= 1e7:
        return f"{v / 1e7:.2f} Cr"
    if abs(v) >= 1e5:
        return f"{v / 1e5:.2f} L"
    return f"{v:,.0f}"


def arrow(pct) -> str:
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return ""
    return "+" if pct > 0 else ("-" if pct < 0 else "")


def fmt_pct_cell(pct) -> str:
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return "-"
    glyph = "\u25b2" if p > 0 else ("\u25bc" if p < 0 else "=")
    return f"{glyph}{abs(p):.2f}%"


def utc_to_ist(ts) -> str:
    if not ts:
        return "-"
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t.astimezone(IST).strftime("%d-%b %H:%M")
    except ValueError:
        return "-"


def nice_date(d) -> str:
    if isinstance(d, dt.date):
        return d.strftime("%d-%b")
    try:
        return dt.datetime.strptime(str(d), "%Y-%m-%d").strftime("%d-%b")
    except ValueError:
        return str(d or "-")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


def tg_send(text: str, dry_run: bool, reply_to: int | None = None) -> int | None:
    """Send an HTML message; split automatically if it exceeds the limit.
    Returns the Telegram message_id of the last chunk (for reply threading)."""
    chunks = []
    rest = text
    while len(rest) > TG_MSG_LIMIT:
        cut = rest.rfind("\n", 0, TG_MSG_LIMIT)
        cut = cut if cut > 500 else TG_MSG_LIMIT
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    chunks.append(rest)

    if dry_run:
        log(f"DRY-RUN: {len(chunks)} message(s) would be sent to chat {TG_CHAT or '<unset>'}:")
        for i, c in enumerate(chunks, 1):
            print(f"\n----- message {i}/{len(chunks)} ({len(c)} chars) -----\n{c}\n----- end -----")
        return None

    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
    for i, chunk in enumerate(chunks, 1):
        payload = {
            "chat_id": TG_CHAT,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if TG_THREAD:
            payload["message_thread_id"] = int(TG_THREAD)
        if reply_to:
            payload["reply_to_message_id"] = int(reply_to)
        r = requests.post(f"{TELEGRAM_API}/bot{TG_TOKEN}/sendMessage",
                          json=payload, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = {"ok": False, "description": f"HTTP {r.status_code}"}
        if r.status_code != 200 or not body.get("ok"):
            raise RuntimeError(f"Telegram send failed: {body.get('description', r.status_code)}")
        msg_id = body.get("result", {}).get("message_id")
        log(f"telegram: message {i}/{len(chunks)} delivered (message_id={msg_id})")
    return msg_id


# --------------------------------------------------------------------------- #
# Data gathering
# --------------------------------------------------------------------------- #


def fetch_live_card(live: MCPClient) -> dict:
    """Indices + VIX + movers from the live MCP server."""
    card: dict = {}

    idx = live.call("cm_get_live_indices", group="derivatives", nameFilter="")
    rows = (idx.get("indices") or {}).get("derivatives") or []
    want = {"NIFTY 50": "NIFTY 50", "NIFTY BANK": "NIFTY BANK",
            "NIFTY FIN SERVICE": "NIFTY FIN SVC"}
    picked = [r for r in rows if r.get("indexName") in want]
    card["nse_ts"] = idx.get("nseTimestamp", "-")
    card["updated_at"] = idx.get("updatedAt")
    card["data_date"] = parse_nse_ts(idx.get("nseTimestamp"))
    card["indices"] = [(want[r["indexName"]], r.get("last"), r.get("percChange"))
                       for r in picked]

    vix = live.call("cm_get_index_quote", indexName="INDIA VIX")
    v = vix.get("index") or {}
    if v:
        card["indices"].append(("INDIA VIX", v.get("last"), v.get("percChange")))

    movers = live.call("nse_get_market_movers", indexName="", limit=TOP_N)
    card["gainers"] = movers.get("gainers") or []
    card["losers"] = movers.get("losers") or []
    return card


def fetch_breadth(bhav: MCPClient, date_str: str) -> dict | None:
    try:
        return bhav.call("get_market_breadth", date=date_str)
    except MCPError as exc:
        log(f"breadth unavailable: {exc}")
        return None


# --------------------------------------------------------------------------- #
# Message builders
# --------------------------------------------------------------------------- #


def build_intraday_message(card: dict, breadth: dict | None, now: dt.datetime) -> str:
    L: list[str] = []
    L.append(f"<b>NSE LIVE SNAPSHOT</b>  {now:%a %d %b %Y | %H:%M IST}")
    L.append("")

    idx_rows = card.get("indices") or []
    if idx_rows:
        lines = [f"{name:<14}{inr(last):>12}  {fmt_pct_cell(pc):>9}"
                 for name, last, pc in idx_rows]
        L.append("<pre>" + esc("\n".join(lines)) + "</pre>")
    else:
        L.append("<i>index data unavailable</i>")

    for title, rows in (("TOP GAINERS", card.get("gainers")),
                        ("TOP LOSERS", card.get("losers"))):
        L.append(f"\n<b>{title}</b>")
        if not rows:
            L.append("<i>unavailable</i>")
            continue
        lines = []
        for r in rows:
            lines.append(
                f"{esc(str(r.get('symbol', '-')))[:14]:<14}"
                f"{inr(r.get('ltp')):>10}  {fmt_pct_cell(r.get('perChange')):>9}"
                f"  {esc(str(r.get('turnover', '-')))[:12]:>12}"
            )
        L.append("<pre>" + esc("\n".join(lines)) + "</pre>")

    if breadth:
        L.append(
            f"\n<b>BREADTH</b> ({esc(nice_date(breadth.get('date')))})  "
            f"{inr(breadth.get('advances', 0), 0)} up / {inr(breadth.get('declines', 0), 0)} down / "
            f"{inr(breadth.get('unchanged', 0), 0)} flat  A/D {breadth.get('ad_ratio', '-')}"
        )

    L.append(
        f"\nNSE data: {esc(str(card.get('nse_ts', '-'))[:17])}"
        f" | MCP refreshed: {esc(utc_to_ist(card.get('updated_at')))} IST"
        f"\n<i>{esc(DISCLAIMER)}</i>"
    )
    return "\n".join(L)


def build_eod_message(data: dict) -> str:
    L: list[str] = []
    data_date = nice_date(data.get("date"))
    stale_note = "" if data.get("is_today") else "  <i>(latest published session)</i>"
    L.append(f"<b>NSE EOD DIGEST</b>  {data_date}{stale_note}")
    L.append("")

    mood = data.get("mood") or {}
    bench = mood.get("benchmarks") or {}
    want = ["Nifty 50", "Nifty Next 50", "Nifty 500",
            "NIFTY Midcap 100", "NIFTY Smallcap 100"]
    display = {"Nifty 50": "NIFTY 50", "Nifty Next 50": "NIFTY NEXT 50",
               "Nifty 500": "NIFTY 500", "NIFTY Midcap 100": "NIFTY MIDCAP 100",
               "NIFTY Smallcap 100": "NIFTY SMALLCAP 100"}
    low = {k.lower(): (k, v) for k, v in bench.items()}
    rows = []
    for name in want:
        hit = low.get(name.lower())
        if hit:
            rows.append((display.get(name, name.upper()),
                         hit[1].get("close"), hit[1].get("change_pct")))
    if rows:
        lines = [f"{n:<18}{inr(c):>12}  {fmt_pct_cell(p):>9}" for n, c, p in rows]
        L.append("<pre>" + esc("\n".join(lines)) + "</pre>")

    br = data.get("breadth") or {}
    if br:
        sb = mood.get("stock_breadth") or {}
        reading = f"  <i>{esc(sb.get('reading', ''))}</i>" if sb.get("reading") else ""
        L.append(
            f"\n<b>BREADTH</b>  {inr(br.get('advances', 0), 0)} up / "
            f"{inr(br.get('declines', 0), 0)} down / {inr(br.get('unchanged', 0), 0)} flat"
            f"  A/D {br.get('ad_ratio', '-')}{reading}"
        )

    vix = mood.get("india_vix") or {}
    if vix.get("level") is not None:
        w = vix.get("vs_1w_ago") or {}
        wtxt = ""
        if w.get("change_pct") is not None:
            wtxt = (f" | 1W {arrow(w.get('change_pct'))}"
                    f"{abs(float(w.get('change_pct'))):.1f}% {esc(w.get('direction', ''))}")
        L.append(f"<b>INDIA VIX</b>  {inr(vix.get('level'))}  "
                 f"{fmt_pct_cell(vix.get('change_1d_pct'))} (1D){wtxt}")

    im = data.get("index_movers") or {}
    if im.get("gainers") or im.get("losers"):
        L.append("\n<b>SECTOR MOVERS</b>")
        up = ", ".join(
            f"{esc(str(g.get('index', '')).replace('Nifty ', ''))} "
            f"{float(g.get('return_pct', 0)):+.2f}%"
            for g in (im.get("gainers") or [])[:3]
        )
        dn = ", ".join(
            f"{esc(str(g.get('index', '')).replace('Nifty ', ''))} "
            f"{float(g.get('return_pct', 0)):+.2f}%"
            for g in (im.get("losers") or [])[:3]
        )
        L.append(f"Up: {up or '-'}\nDown: {dn or '-'}")

    for title, rows2 in (("TOP GAINERS", (data.get("top_gain") or {}).get("stocks")),
                         ("TOP LOSERS", (data.get("top_loss") or {}).get("stocks"))):
        L.append(f"\n<b>{title}</b>")
        if not rows2:
            L.append("<i>unavailable</i>")
            continue
        lines = []
        for r in rows2:
            lines.append(
                f"{esc(str(r.get('symbol', '-')))[:14]:<14}"
                f"{inr(r.get('close')):>10}  {fmt_pct_cell(r.get('pct_change')):>9}"
                f"  vol {qty_str(r.get('volume')):>10}"
            )
        L.append("<pre>" + esc("\n".join(lines)) + "</pre>")

    act = (data.get("active") or {}).get("stocks") or []
    if act:
        L.append("\n<b>MOST ACTIVE (turnover)</b>")
        lines = []
        for r in act:
            cr = float(r.get("traded_value", 0)) / 1e7
            lines.append(
                f"{esc(str(r.get('symbol', '-')))[:14]:<14}"
                f"{inr(r.get('close')):>10}  {fmt_pct_cell(r.get('pct_change')):>9}"
                f"  {cr:>10,.1f} Cr"
            )
        L.append("<pre>" + esc("\n".join(lines)) + "</pre>")

    L.append(f"\n<i>{esc(DISCLAIMER)}</i>")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #


def run_intraday(force: bool, dry_run: bool) -> int:
    now = ist_now()
    today = now.date()

    live = MCPClient(LIVE_MCP_URL, "live-mcp")
    live.start()
    card = fetch_live_card(live)

    # staleness guard: if NSE data is not from today, this is a holiday or a
    # special non-trading day missing from the list -> stay silent.
    data_date = card.get("data_date")
    if not force and data_date and data_date != today:
        log(f"SKIP: live data is from {data_date} (today is {today}) - non-trading day")
        return 0

    bhav = None
    breadth = None
    try:
        bhav = MCPClient(BHAV_MCP_URL, "bhavcopy-mcp")
        bhav.start()
        breadth = fetch_breadth(bhav, today.isoformat())
    except MCPError as exc:
        log(f"breadth source unavailable: {exc}")

    msg = build_intraday_message(card, breadth, now)
    log(f"intraday card built ({len(msg)} chars)")
    tg_send(msg, dry_run)

    # ---- Phase A+B scanner ------------------------------------------------#
    import scanner
    last_send = {"t": 0.0}

    def send(text: str, reply_to: int | None = None):
        gap = time.time() - last_send["t"]
        if gap < 1.1:
            time.sleep(1.1 - gap)
        last_send["t"] = time.time()
        return tg_send(text, dry_run, reply_to=reply_to)

    # ---- hourly module: breadth / VIX / F&O movers at XX:30 IST ----------#
    # (minute >= 30 covers delayed cron triggers; per-hour dedupe in state)
    if now.minute >= 30 or force:
        try:
            import hourly
            hourly.run_hourly(live, send, now, dry_run)
        except Exception as exc:  # noqa: BLE001
            log(f"hourly module failed: {exc}")

    log("scanner (Phase A+B) starting ...")
    return scanner.run_scan(live, bhav, send, now, dry_run)


def run_premarket_mode(force: bool, dry_run: bool) -> int:
    now = ist_now()
    live = MCPClient(LIVE_MCP_URL, "live-mcp")
    live.start()
    last_send = {"t": 0.0}

    def send(text: str, reply_to: int | None = None):
        gap = time.time() - last_send["t"]
        if gap < 1.1:
            time.sleep(1.1 - gap)
        last_send["t"] = time.time()
        return tg_send(text, dry_run, reply_to=reply_to)

    import premarket
    return premarket.run_premarket(live, send, now, dry_run, force)


def run_eod(force: bool, dry_run: bool) -> int:
    now = ist_now()
    today = now.date()

    bhav = MCPClient(BHAV_MCP_URL, "bhavcopy-mcp")
    bhav.start()

    breadth = None
    for attempt in range(1, BHAVCOPY_POLL_TRIES + 1):
        breadth = fetch_breadth(bhav, today.isoformat())
        if breadth and (str(breadth.get("date")) == today.isoformat() or force):
            break
        if attempt < BHAVCOPY_POLL_TRIES:
            log(f"bhavcopy for {today} not published yet "
                f"({attempt}/{BHAVCOPY_POLL_TRIES}); waiting {BHAVCOPY_POLL_WAIT}s")
            time.sleep(BHAVCOPY_POLL_WAIT)
        else:
            log(f"bhavcopy for {today} not published after {BHAVCOPY_POLL_TRIES} attempts")

    if not breadth:
        log("SKIP: bhavcopy MCP unreachable")
        return 0

    data_date = str(breadth.get("date"))
    if data_date != today.isoformat() and not force:
        if ist_now().time() >= EOD_STRICT_UNTIL:
            log(f"SKIP: today's bhavcopy still missing at {ist_now():%H:%M} IST "
                f"(latest: {data_date}) - probably a non-trading day")
            return 0
        log(f"NOTE: digest built from latest published session ({data_date}); "
            f"today's file not detected yet")

    log("fetching digest sections from bhavcopy MCP ...")
    data: dict = {"breadth": breadth, "date": data_date,
                  "is_today": data_date == today.isoformat()}

    def safe(tool: str, key: str, **args):
        try:
            data[key] = bhav.call(tool, **args)
        except MCPError as exc:
            log(f"{tool} failed: {exc}")
            data[key] = {}

    safe("get_market_mood", "mood", date=data_date)
    safe("get_index_movers", "index_movers", date=data_date, period="1D", n=3, scope="equity")
    safe("get_top_movers", "top_gain", date=data_date, n=TOP_N, direction="gain")
    safe("get_top_movers", "top_loss", date=data_date, n=TOP_N, direction="loss")
    safe("get_top_by_volume", "active", date=data_date, n=TOP_N, sortBy="value")

    msg = build_eod_message(data)
    log(f"EOD digest built ({len(msg)} chars)")
    tg_send(msg, dry_run)
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description="NSE MCP -> Telegram snapshot bot")
    ap.add_argument("--mode", choices=["intraday", "eod", "premarket"], required=True)
    ap.add_argument("--force", action="store_true",
                    help="bypass holiday / window / staleness gates (testing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the message instead of sending to Telegram")
    args = ap.parse_args()

    log(f"mode={args.mode} force={args.force} dry_run={args.dry_run}")
    skip = gate_check(args.mode, args.force)
    if skip:
        log(f"SKIP: {skip}")
        return 0

    try:
        if args.mode == "intraday":
            return run_intraday(args.force, args.dry_run)
        if args.mode == "premarket":
            return run_premarket_mode(args.force, args.dry_run)
        return run_eod(args.force, args.dry_run)
    except MCPError as exc:
        log(f"FAIL: MCP error: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
