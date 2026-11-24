"""Static universe data for the Phase A+B scanner.

ACCURACY NOTES (shown in Telegram where relevant):
- Sector membership = NSE F&O stocks grouped by the sector index they belong
  to. The F&O universe changes monthly and this table is embedded, not live:
  symbols that are no longer listed/F&O are silently skipped because quotes
  are matched against live data. Update this table when NSE revises the list.
- Verified against the live feed on 2026-11-23: GUJGASLTD, ICICIPRULI and
  LTIM are not returned by cm_get_equity_stocks (feed omission, not our
  filter) and TATAMOTORS is delisted/renamed TMPV. All four are skipped
  gracefully: sector breadth uses quoted members only, and index weight
  coverage % shrinks accordingly.
- Index weights are approximate public free-float weights, used ONLY for the
  mover-contribution factor. Coverage % is computed at runtime and reported.
"""
from __future__ import annotations

# display -> live NSE index name (verified against mcp.nseindia.in 2026-11-23)
SECTOR_INDEX = {
    "Bank": "NIFTY BANK",
    "Fin Services": "NIFTY FIN SERVICE",
    "IT": "NIFTY IT",
    "Auto": "NIFTY AUTO",
    "FMCG": "NIFTY FMCG",
    "Pharma": "NIFTY PHARMA",
    "Metal": "NIFTY METAL",
    "Realty": "NIFTY REALTY",
    "Energy": "NIFTY ENERGY",
    "Oil & Gas": "NIFTY OIL AND GAS",
    "PSU Bank": "NIFTY PSU BANK",
    "Private Bank": "NIFTY PVT BANK",
    "Media": "NIFTY MEDIA",
    "Consumer Durables": "NIFTY CONSR DURBL",
    "Healthcare": "NIFTY HEALTHCARE",
}

SECTOR_MEMBERS: dict[str, list[str]] = {
    "Bank": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
             "INDUSINDBK", "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "PNB",
             "BANKBARODA", "CANBK", "AUBANK"],
    "Fin Services": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
                     "BAJFINANCE", "BAJAJFINSV", "JIOFIN", "SBILIFE",
                     "HDFCLIFE", "ICICIPRULI", "ICICIGI", "PFC", "RECLTD",
                     "CHOLAFIN", "LICHSGFIN", "MUTHOOTFIN", "MANAPPURAM",
                     "SBICARD", "BAJAJHLDNG"],
    "IT": ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM", "PERSISTENT",
           "COFORGE", "MPHASIS", "OFSS", "KPITTECH", "TATAELXSI"],
    "Auto": ["MARUTI", "M&M", "TMPV", "BAJAJ-AUTO", "EICHERMOT",
             "HEROMOTOCO", "TVSMOTOR", "ASHOKLEY", "BOSCHLTD", "UNOMINDA",
             "SONACOMS", "BHARATFORG", "EXIDEIND", "ESCORTS"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO",
             "GODREJCP", "COLPAL", "TATACONSUM", "EMAMILTD", "UBL", "RADICO",
             "PATANJALI", "VBL"],
    "Pharma": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN",
               "AUROPHARMA", "TORNTPHARM", "ALKEM", "GLENMARK", "IPCALAB",
               "ZYDUSLIFE", "MANKIND", "LAURUSLABS"],
    "Metal": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL",
              "SAIL", "NATIONALUM", "HINDZINC", "APLAPOLLO", "WELCORP",
              "JINDALSAW", "RATNAMANI"],
    "Realty": ["DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "LODHA", "NCC",
               "BRIGADE", "PHOENIXLTD", "ANANTRAJ"],
    "Energy": ["RELIANCE", "ONGC", "NTPC", "POWERGRID", "ADANIGREEN",
               "ADANIPOWER", "TATAPOWER", "JSWENERGY", "NHPC", "TORNTPOWER",
               "CESC"],
    "Oil & Gas": ["ONGC", "RELIANCE", "IOC", "BPCL", "GAIL", "PETRONET",
                  "HINDPETRO", "OIL", "ADANIENT", "ATGL", "GUJGASLTD"],
    "PSU Bank": ["SBIN", "PNB", "BANKBARODA", "CANBK", "UNIONBANK", "INDIANB",
                 "MAHABANK", "IOB", "BANKINDIA", "CENTRALBK"],
    "Private Bank": ["HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK",
                     "INDUSINDBK", "AUBANK", "FEDERALBNK", "BANDHANBNK",
                     "IDFCFIRSTB", "YESBANK", "RBLBANK"],
    "Media": ["ZEEL", "SUNTV", "PVRINOX", "SAREGAMA", "NAZARA", "NETWORK18"],
    "Consumer Durables": ["TITAN", "POLYCAB", "HAVELLS", "VOLTAS",
                          "BLUESTARCO", "DIXON", "CROMPTON", "WHIRLPOOL",
                          "SYMPHONY"],
    "Healthcare": ["APOLLOHOSP", "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB",
                   "TORNTPHARM", "MAXHEALTH", "FORTIS", "NH", "MEDANTA",
                   "LALPATHLAB", "METROPOLIS", "SYNGENE"],
}

# Spec index models. SENSEX dropped: BSE, not covered by NSE MCP.
# live_name must match cm_get_live_indices output exactly.
INDEX_MODELS = {
    "NIFTY": {"live": "NIFTY 50"},
    "BANKNIFTY": {"live": "NIFTY BANK", "sector": "Bank"},
    "FINNIFTY": {"live": "NIFTY FIN SERVICE", "sector": "Fin Services"},
    "MIDCPNIFTY": {"live": "NIFTY MID SELECT"},
}

# Approximate free-float weights (%, public data, semi-annual revision).
# Used only for the mover-contribution factor; coverage reported at runtime.
INDEX_WEIGHTS: dict[str, dict[str, float]] = {
    "NIFTY 50": {
        "HDFCBANK": 13.4, "ICICIBANK": 9.6, "RELIANCE": 8.9, "INFY": 4.9,
        "BHARTIARTL": 4.3, "TCS": 3.8, "SBIN": 3.3, "ITC": 3.0, "LT": 2.7,
        "AXISBANK": 2.7, "KOTAKBANK": 2.4, "BAJFINANCE": 2.3, "M&M": 2.1,
        "HINDUNILVR": 2.0, "SUNPHARMA": 1.9, "LICI": 1.8, "MARUTI": 1.7,
        "TITAN": 1.5, "TATAMOTORS": 1.4, "HCLTECH": 1.4,
    },
    "NIFTY BANK": {
        "HDFCBANK": 27.0, "ICICIBANK": 22.0, "SBIN": 10.0, "AXISBANK": 8.4,
        "KOTAKBANK": 7.6, "INDUSINDBK": 4.4, "PNB": 4.0, "BANKBARODA": 3.5,
        "AUBANK": 3.0, "CANBK": 3.0, "FEDERALBNK": 2.5, "IDFCFIRSTB": 2.0,
        "BANDHANBNK": 1.6,
    },
    "NIFTY FIN SERVICE": {
        "HDFCBANK": 21.5, "ICICIBANK": 17.0, "SBIN": 8.0, "BAJFINANCE": 7.0,
        "KOTAKBANK": 6.0, "AXISBANK": 5.6, "BAJAJFINSV": 3.9, "JIOFIN": 3.0,
        "SBILIFE": 3.4, "HDFCLIFE": 3.4, "ICICIPRULI": 2.5, "ICICIGI": 2.4,
        "PFC": 2.4, "RECLTD": 2.0, "CHOLAFIN": 1.9, "LICHSGFIN": 1.6,
        "MUTHOOTFIN": 1.5, "MANAPPURAM": 1.0, "SBICARD": 1.3,
        "BAJAJHLDNG": 1.6,
    },
    "NIFTY MID SELECT": {
        "CUMMINSIND": 3.4, "POLYCAB": 3.2, "BHARATFORG": 3.0, "DIXON": 2.8,
        "PERSISTENT": 2.6, "COFORGE": 2.4, "FORTIS": 2.2, "MAXHEALTH": 2.1,
        "ASHOKLEY": 2.0, "CROMPTON": 1.6, "MPHASIS": 1.5, "HINDZINC": 2.4,
        "FEDERALBNK": 2.4, "AUBANK": 2.4, "PRESTIGE": 1.8,
    },
}

FNO_ALL: list[str] = sorted({s for members in SECTOR_MEMBERS.values() for s in members})
UNIVERSE_LETTERS: list[str] = sorted({s[0] for s in FNO_ALL})

DISCLAIMER = ("Educational, not investment advice. Weights/universe are "
              "approximate embedded data; option-chain, F&O OI, ban list and "
              "market depth are not available from the NSE MCP endpoints.")
