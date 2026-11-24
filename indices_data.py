"""Index constituent + weight tables for the hourly breadth module.

SOURCES / ACCURACY:
- NIFTY 50: full 50-stock free-float weight table (stockmojo.in/index-weightage,
  as of 23-Nov-2026; cross-checked against NSE factsheet ranking 12-Nov-2026).
- NIFTY BANK / NIFTY FIN SERVICE / NIFTY MID SELECT: approximate free-float
  weights embedded with the scanner universe (universe.py, verified 2026-11-23).
- All weights are normalised at runtime; weight coverage % is reported on the
  card whenever it is below 95%, per the bot's honesty rules.
- NSE index lists are revised periodically: symbols that no longer exist or
  are missing from the live feed are silently skipped (coverage % shrinks).
"""
from __future__ import annotations

NIFTY50_W: dict[str, float] = {
    "HDFCBANK": 10.54, "ICICIBANK": 8.81, "RELIANCE": 7.52, "BHARTIARTL": 5.12,
    "LT": 4.17, "SBIN": 3.79, "INFY": 3.41, "AXISBANK": 3.32, "KOTAKBANK": 2.84,
    "BAJFINANCE": 2.53, "M&M": 2.51, "ITC": 2.43, "ETERNAL": 2.26, "TCS": 1.97,
    "SUNPHARMA": 1.88, "TITAN": 1.84, "HINDUNILVR": 1.57, "TATASTEEL": 1.48,
    "MARUTI": 1.47, "NTPC": 1.46, "ADANIPORTS": 1.36, "BEL": 1.32,
    "HINDALCO": 1.32, "SHRIRAMFIN": 1.29, "BSE": 1.24, "HCLTECH": 1.22,
    "ULTRACEMCO": 1.22, "JSWSTEEL": 1.17, "POWERGRID": 1.13, "BAJAJ-AUTO": 1.12,
    "GRASIM": 1.11, "INDIGO": 1.05, "ASIANPAINT": 1.04, "COALINDIA": 0.97,
    "BAJAJFINSV": 0.96, "ADANIENT": 0.96, "EICHERMOT": 0.94, "NESTLEIND": 0.92,
    "TECHM": 0.91, "ONGC": 0.85, "APOLLOHOSP": 0.85, "TRENT": 0.83,
    "SBILIFE": 0.74, "CIPLA": 0.74, "DRREDDY": 0.73, "MAXHEALTH": 0.70,
    "JIOFIN": 0.68, "TATACONSUM": 0.60, "TMPV": 0.56, "HDFCLIFE": 0.54,
}

BANK_W: dict[str, float] = {
    "HDFCBANK": 27.0, "ICICIBANK": 22.0, "SBIN": 10.0, "AXISBANK": 8.4,
    "KOTAKBANK": 7.6, "INDUSINDBK": 4.4, "PNB": 4.0, "BANKBARODA": 3.5,
    "AUBANK": 3.0, "CANBK": 3.0, "FEDERALBNK": 2.5, "IDFCFIRSTB": 2.0,
    "BANDHANBNK": 1.6,
}

FIN_W: dict[str, float] = {
    "HDFCBANK": 21.5, "ICICIBANK": 17.0, "SBIN": 8.0, "BAJFINANCE": 7.0,
    "KOTAKBANK": 6.0, "AXISBANK": 5.6, "BAJAJFINSV": 3.9, "JIOFIN": 3.0,
    "SBILIFE": 3.4, "HDFCLIFE": 3.4, "ICICIPRULI": 2.5, "ICICIGI": 2.4,
    "PFC": 2.4, "RECLTD": 2.0, "CHOLAFIN": 1.9, "LICHSGFIN": 1.6,
    "MUTHOOTFIN": 1.5, "MANAPPURAM": 1.0, "SBICARD": 1.3, "BAJAJHLDNG": 1.6,
}

# Proxy for midcap breadth: NIFTY MID SELECT (Midcap 100's full constituent
# list is not available from the MCP and could not be embedded verified).
MID_W: dict[str, float] = {
    "CUMMINSIND": 3.4, "POLYCAB": 3.2, "BHARATFORG": 3.0, "DIXON": 2.8,
    "PERSISTENT": 2.6, "COFORGE": 2.4, "FORTIS": 2.2, "MAXHEALTH": 2.1,
    "ASHOKLEY": 2.0, "CROMPTON": 1.6, "MPHASIS": 1.5, "HINDZINC": 2.4,
    "FEDERALBNK": 2.4, "AUBANK": 2.4, "PRESTIGE": 1.8,
}

# rows shown in the hourly breadth table, in order
BREADTH_TABLES: list[dict] = [
    {"key": "NIFTY 50", "live": "NIFTY 50", "members": NIFTY50_W,
     "note": ""},
    {"key": "NIFTY BANK", "live": "NIFTY BANK", "members": BANK_W,
     "note": "weights approx"},
    {"key": "NIFTY FIN SERVICE", "live": "NIFTY FIN SERVICE", "members": FIN_W,
     "note": "weights approx"},
    {"key": "NIFTY MID SELECT", "live": "NIFTY MID SELECT", "members": MID_W,
     "note": "proxy for Midcap 100 breadth"},
]

ALL_MEMBERS: set[str] = {
    s for t in BREADTH_TABLES for s in t["members"]}
BREADTH_LETTERS: list[str] = sorted({s[0] for s in ALL_MEMBERS})
