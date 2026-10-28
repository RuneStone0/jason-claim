#!/usr/bin/env python3
"""Jason's tweet claim — monthly standing report + event alerts.

THE CLAIM (x.com/Jason/status/2102051827671871601, posted 2026-09-21 15:06 UTC):
  (A) "90% chance that $QQQ and top AI companies will beat $BTC over the next
      12, 24 and 36 months"
  (B) "Bitcoin won't break its record high in next 12 months — sideways for a
      bit more boys"

So there are four checkable legs:
  A12/A24/A36 — at the 12/24/36-month marks, do QQQ *and* the AI basket have a
                higher total return than BTC?   (he needs BOTH to beat BTC)
  B           — does BTC stay below its record high until 2027-09-21?

MODES
  --monthly   standing report by email (1st of the month). Idempotent per month.
  --daily     cheap check: (1) has BTC broken its record high? (2) has a leg's
              milestone date arrived undecided? Emails only on an event.
  --status    print the standing, no email, no state change.

TEST HOOKS
  DRY_RUN=1       render + print instead of sending
  TODAY=YYYY-MM-DD  pretend it is this date
  FORCE=1         ignore the "already sent/notified" guards

DATA SOURCES (all keyless; every one verified live from this host 2026-09-26)
  equities/ETFs : Nasdaq historical API (primary), Yahoo chart (fallback).
                  Yahoo 429s this host under burst — it is a FALLBACK only.
  BTC spot      : CoinGecko simple/price.
  BTC highs     : CoinGecko OHLC 4h candles (7d window) + Kraken daily OHLC
                  (720-day window, the durable one for "has it broken yet").
  record high   : FROZEN constant — an ATH is only meaningful as of the tweet
                  date. CoinGecko $126,080.00 intraday 2025-10-06T10:57Z;
                  Kraken daily high the same day $126,198.10 (0.1% apart).

BASELINE CONVENTION: the closing price on the tweet's date (2026-09-21) for
everything, source-stamped below. Frozen as constants so the comparison can
never drift with a data-source revision.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ----------------------------------------------------------------- constants

TWEET_ID = "2102051827671871601"
TWEET_URL = "https://x.com/Jason/status/2102051827671871601"
TWEET_DATE = "2026-09-21"          # decoded from the snowflake ID: 15:06:24Z
POSTED_BY = "@Jason"

MILESTONES = [("12", "2027-09-21"), ("24", "2028-09-21"), ("36", "2029-09-21")]
WINDOW_END_B = "2027-09-21"        # claim B's 12-month window

# ---- Jason's SECOND tweet (2026-10-27) — "A brutal year for Bitcoin…" ----
TWEET2_ID = "2114956980674605075"
TWEET2_URL = "https://x.com/jason/status/2114956980674605075"
TWEET2_DATE = "2026-10-27"          # decoded from the snowflake ID: 05:46:52Z
# Reference on Jason's bearish track record (Rune, 2026-10-27):
# AlexesNakamoto's "11 years. Still waiting for zero 🤡".
REF_URL = "https://x.com/AlexesNakamoto/status/2115053956355231814"

# Claim C: "We will test the 57k low again in the next 12 months."
# Trigger (Rune, 2026-10-27): BTC intraday low AT/BELOW $57,000. BTC's actual
# 2026 low was $57,740 (2026-07-01); "57k" is Jason's round number.
CLAIM_C_57K = 57000.00
WINDOW_END_C = "2027-10-27"         # 12 months after the tweet

# Claim D: "Bitcoin will never break its all-time high again."
# Permanent (no end date). Lost the moment BTC breaks $126,080 after 2026-10-27;
# it can only ever be "still holding", never "won" in finite time.

# Alfred's interpretation of "top AI companies": equal-weight, largest
# AI-exposed listed companies. Fixed and documented so the number is
# reproducible; Rune can change the basket.
# SPCX added 2026-09-26 on Rune's instruction ("SpaceX ... is now also a part of
# the AI race with xAI") — SpaceX acquired xAI in Feb 2026 and IPO'd on Nasdaq in
# June 2026, so it is a tradeable price, not a private-mark estimate.
# ⚠️ SPCX is a Nasdaq-100 constituent, so QQQ already holds it — the two lines
# overlap by design and the report says so.
AI_BASKET = ["NVDA", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "AMD", "TSM", "SPCX"]
MAGS = "MAGS"                      # cross-check: mcap-weighted Mag-7 ETF
QQQ = "QQQ"
SPY = "SPY"                        # context only, not part of the claim

# ---- money-debasement inputs (Rune's question, 2026-09-26) ------------------
# US CPI-U (CPIAUCSL, seasonally adjusted) is the sanctioned "unit shrinkage"
# series; the BLS public API is a keyless cross-check (CUUR0000SA0, NSA).
# Base month = the last CPI print published BEFORE the claim (Aug 2026 prints
# land ~Sep 10; the tweet is Sep 21). Frozen so the real-return maths is
# reproducible; the base label is printed in every report.
CPI_BASE_MONTH = "2026-08"
CPI_URL_FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
CPI_URL_BLS = ("https://api.bls.gov/publicAPI/v1/timeseries/data/"
               "CUUR0000SA0")
# M2 (broader money) deliberately NOT tracked — Rune, 2026-09-26: "CPI is enough".
# It was FRED-only and FRED refuses this host, so it bought nothing but log noise.
# BTC issuance is a protocol constant, not a forecast: post-April-2024 subsidy
# is 3.125 BTC per ~10-minute block.
BTC_SUBSIDY = 3.125
BTC_BLOCKS_PER_YEAR = 144 * 365
BTC_HALVING_YEAR = 2028


# Frozen closes on 2026-09-21 (Nasdaq historical API, Close/Last column).
BASE = {
    "QQQ": 741.47, "MAGS": 72.97, "SPY": 773.50,
    "NVDA": 227.38, "MSFT": 501.61, "GOOGL": 354.97, "AMZN": 258.45,
    "META": 741.25, "AVGO": 362.66, "AMD": 615.52, "TSM": 445.14,
    "SPCX": 151.85,          # SpaceX Class A (SpaceX+xAI), Nasdaq, from 2026-06-12
}
# Frozen BTC value for 2026-09-21 (CoinGecko daily).
BASE_BTC = 81169.04
# Frozen record high to beat (claim B).
BTC_ATH = 126080.00
BTC_ATH_DATE = "2025-10-06"
BTC_ATH_ALT = 126198.10            # Kraken daily-high cross-check, same day

NASDAQ_CLASS = {
    "QQQ": "etf", "MAGS": "etf", "SPY": "etf",
    "NVDA": "stocks", "MSFT": "stocks", "GOOGL": "stocks", "AMZN": "stocks",
    "META": "stocks", "AVGO": "stocks", "AMD": "stocks", "TSM": "stocks",
    "SPCX": "stocks",
}

STATE_DIR = Path("/tmp/jason-claim")   # scrubbed for CI (no /opt/data on runners)
STATE = STATE_DIR / "state.json"
REPORTS = STATE_DIR / "reports"
LOG = STATE_DIR / "monitor.log"
SENDER = "/opt/data/profiles/alfred/scripts/send_report_html.py"
RICH_SENDER = "/opt/data/profiles/alfred/scripts/send_report_rich.py"
SHOT = "/opt/data/scripts/html_shot.py"
DASH_MODULE = "/opt/data/scripts/jason_claim_dashboard.py"
RECIPIENTS = ""  # scrubbed for public repo (email reminders run on the local box only)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")


# ----------------------------------------------------------------- utilities

def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{stamp} {msg}\n")


def today() -> dt.date:
    env = os.environ.get("TODAY", "").strip()
    if env:
        return dt.date.fromisoformat(env)
    return dt.datetime.now(dt.timezone.utc).date()


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8")


def http(url: str, timeout: int = 25, tries: int = 3, delay: int = 4,
         headers: dict = None) -> str:
    last = None
    for i in range(tries):
        try:
            hdr = {"User-Agent": UA,
                   "Accept": "application/json, text/plain, */*"}
            hdr.update(headers or {})
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:            # noqa: BLE001 — any failure -> retry
            last = e
            if i < tries - 1:
                time.sleep(delay)
    raise last


def iso(ts: int) -> str:
    """Unix timestamp -> ISO date. Accepts seconds OR milliseconds.

    CoinGecko returns milliseconds; Kraken returns seconds. Getting this wrong
    silently drops a whole data source (year 58688 is not a date)."""
    ts = int(ts)
    if ts > 10 ** 11:                 # >= year 5138 in seconds => milliseconds
        ts //= 1000
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()


def pct(v: float) -> str:
    return f"{v:+.2f}%"


# ----------------------------------------------------------------- data sources

def btc_spot() -> float:
    """CoinGecko spot BTC/USD."""
    d = json.loads(http("https://api.coingecko.com/api/v3/simple/price"
                        "?ids=bitcoin&vs_currencies=usd"))
    return float(d["bitcoin"]["usd"])


def btc_daily(days: int) -> dict:
    """{iso_date: price} from CoinGecko daily market_chart."""
    d = json.loads(http("https://api.coingecko.com/api/v3/coins/bitcoin/"
                        f"market_chart?vs_currency=usd&days={days}&interval=daily",
                        timeout=40))
    out = {}
    for ts, price in d["prices"]:
        out[iso(int(ts))] = float(price)
    return out


def btc_highs_coingecko(days: int = 7) -> dict:
    """{iso_date: max high} from CoinGecko 4h OHLC candles (7d max on free tier)."""
    d = json.loads(http("https://api.coingecko.com/api/v3/coins/bitcoin/"
                        f"ohlc?vs_currency=usd&days={days}", timeout=40))
    out = {}
    for row in d:                     # [ts, open, high, low, close]
        day, high = iso(int(row[0])), float(row[2])
        out[day] = max(out.get(day, 0.0), high)
    return out


def btc_lows_kraken() -> dict:
    """{iso_date: low} from Kraken daily candles (~720d window)."""
    d = json.loads(http("https://api.kraken.com/0/public/OHLC"
                        "?pair=XBTUSD&interval=1440", timeout=40))
    if d.get("error"):
        raise RuntimeError(f"kraken: {d['error']}")
    res = [v for k, v in d["result"].items() if k != "last"][0]
    return {iso(int(r[0])): float(r[3]) for r in res}


def btc_lows_coingecko(days: int = 7) -> dict:
    """{iso_date: min low} from CoinGecko 4h OHLC candles."""
    d = json.loads(http("https://api.coingecko.com/api/v3/coins/bitcoin/"
                        f"ohlc?vs_currency=usd&days={days}", timeout=40))
    out = {}
    for row in d:                     # [ts, open, high, low, close]
        day, low = iso(int(row[0])), float(row[3])
        out[day] = min(out.get(day, low), low)
    return out


def btc_highs_kraken() -> dict:
    """{iso_date: high} from Kraken daily candles (~720d window)."""
    d = json.loads(http("https://api.kraken.com/0/public/OHLC"
                        "?pair=XBTUSD&interval=1440", timeout=40))
    if d.get("error"):
        raise RuntimeError(f"kraken: {d['error']}")
    res = [v for k, v in d["result"].items() if k != "last"][0]
    return {iso(int(r[0])): float(r[2]) for r in res}


NASDAQ_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def curl_text(url: str, timeout: int = 30) -> str:
    """Fetch via the curl binary.

    Some hosts serve curl but reset urllib's TLS/HTTP2 handshake from this
    container (Yahoo, FRED). curl is present in the image and used by other
    fleet scripts, so it is the fallback that actually works.
    """
    p = subprocess.run(["curl", "-sS", "-m", str(timeout),
                        "--http1.1", "-A", UA, url],
                       capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"curl rc={p.returncode} {p.stderr.strip()[:120]}")
    return p.stdout


def fred_monthly(series_id: str) -> dict:
    """{'YYYY-MM': value} from a FRED series CSV.

    FRED is served over curl-only from here (urllib gets a connection reset),
    and it now sometimes refuses curl too — so this is a fallback, not primary.
    """
    txt = None
    for fetcher in (curl_text,
                    lambda u: http(u, timeout=45, tries=2)):
        try:
            txt = fetcher(f"https://fred.stlouisfed.org/graph/fredgraph.csv"
                          f"?id={series_id}&cosd=2024-01-01")
            if txt.strip():
                break
        except Exception as e:        # noqa: BLE001
            log(f"FRED {series_id} via {fetcher}: {repr(e)[:80]}")
            txt = None
    if not txt:
        raise RuntimeError("FRED unreachable")
    out = {}
    for line in txt.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2 or parts[1].strip() in (".", ""):
            continue
        try:
            out[parts[0][:7]] = float(parts[1])
        except ValueError:
            continue
    return out


def bls_cpi_monthly(series_id: str = "CUSR0000SA0") -> dict:
    """CPI-U from the keyless BLS public API.

    CUSR0000SA0 is seasonally adjusted and returns byte-identical values to
    FRED's CPIAUCSL (both 334.131 for 2026-08, verified 2026-09-26), and this
    host can actually reach api.bls.gov — so BLS is the primary source.
    Missing observations come back as the literal "-": skip them, do not cast.
    """
    d = json.loads(http("https://api.bls.gov/publicAPI/v1/timeseries/data/"
                        f"{series_id}", timeout=45))
    if d.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS status {d.get('status')}")
    out = {}
    for row in d["Results"]["series"][0]["data"]:
        try:
            out[f"{row['year']}-{row['period'][1:]}"] = float(row["value"])
        except (ValueError, KeyError):
            continue
    if not out:
        raise RuntimeError("BLS returned no numeric observations")
    return out


def inflation_block() -> dict:
    """How much of a USD return is just the unit shrinking.

    Both sides of this bet are priced in dollars, so debasement inflates BOTH
    nominal returns; the honest use of it is to show each asset's return in
    constant purchasing power (and to say plainly that it does not, by itself,
    advantage BTC in a same-unit comparison).
    """
    blk = {"base_month": CPI_BASE_MONTH, "source": None}
    cpi = {}
    attempts = [("BLS CUSR0000SA0 (SA)", "CUSR0000SA0"),
                ("BLS CUUR0000SA0 (NSA)", "CUUR0000SA0")]
    for label, sid in attempts:
        try:
            cpi = bls_cpi_monthly(sid)
            blk["source"] = label
            break
        except Exception as e:        # noqa: BLE001
            log(f"CPI {label} failed: {repr(e)[:120]}")
    if not cpi:
        try:
            cpi = fred_monthly("CPIAUCSL")
            blk["source"] = "FRED CPIAUCSL (SA)"
        except Exception as e:        # noqa: BLE001
            log(f"FRED CPI failed: {repr(e)[:120]}")
    months = sorted(cpi)
    if not months or CPI_BASE_MONTH not in cpi:
        blk["error"] = "no CPI base month"
        return blk
    latest = months[-1]
    base_v, last_v = cpi[CPI_BASE_MONTH], cpi[latest]
    blk.update({"latest_month": latest, "base_value": base_v,
                "latest_value": last_v,
                "cum_pct": (last_v / base_v - 1.0) * 100.0})
    y, m = int(latest[:4]) - 1, latest[5:]
    prev = f"{y}-{m}"
    if prev in cpi:
        blk["yoy_pct"] = (last_v / cpi[prev] - 1.0) * 100.0
    return blk


def real_return(nominal_pct: float, cum_infl_pct: float):
    """Nominal % -> constant-purchasing-power % (Fisher, compounding)."""
    return ((1.0 + nominal_pct / 100.0) / (1.0 + cum_infl_pct / 100.0) - 1.0) * 100.0


def btc_supply() -> dict:
    """Circulating supply + annual issuance. Protocol constants, not estimates."""
    d = json.loads(http("https://api.coingecko.com/api/v3/coins/bitcoin"
                        "?localization=false&tickers=false&market_data=true"
                        "&community_data=false&developer_data=false", timeout=40))
    supply = float(d["market_data"]["circulating_supply"])
    issued = BTC_SUBSIDY * BTC_BLOCKS_PER_YEAR
    return {"circulating": supply, "annual_issuance": issued,
            "annual_pct": issued / supply * 100.0,
            "subsidy": BTC_SUBSIDY, "halving_year": BTC_HALVING_YEAR}


def nasdaq_realtime(sym: str) -> dict:
    """Last-resort {today_iso: current_price} from the Nasdaq quote/info API.

    Used when the historical feed for a symbol is down (a vendor error returns
    data:null). The price is real-time (intraday) rather than a close, so it is
    injected only as a fallback and logged as such."""
    cls = NASDAQ_CLASS.get(sym, "stocks")
    try:
        d = json.loads(http(f"https://api.nasdaq.com/api/quote/{sym}/info"
                            f"?assetclass={cls}", headers=NASDAQ_HEADERS, tries=2))
        p = (((d.get("data") or {}).get("primaryData") or {})
             .get("lastSalePrice"))
        if not p:
            return {}
        price = float(str(p).replace("$", "").replace(",", "").strip())
        return {today().isoformat(): price}
    except Exception as e:            # noqa: BLE001
        log(f"nasdaq realtime {sym} failed: {repr(e)[:120]}")
        return {}


def nasdaq_closes(syms: list, frm: dt.date, to: dt.date) -> dict:
    """{sym: {iso_date: close}} from the Nasdaq historical API.

    If a symbol's historical feed fails (vendor error -> data:null), fall back to
    the real-time quote so the return calc is not silently dropped."""
    out = {}
    for s in syms:
        cls = NASDAQ_CLASS.get(s, "stocks")
        url = (f"https://api.nasdaq.com/api/quote/{s}/historical?assetclass={cls}"
               f"&fromdate={frm.isoformat()}&todate={to.isoformat()}&limit=9999")
        series = {}
        try:
            d = json.loads(http(url, headers=NASDAQ_HEADERS))
            rows = d["data"]["tradesTable"]["rows"]
            for r in rows:
                m, dd, y = r["date"].split("/")
                series[f"{y}-{m.zfill(2)}-{dd.zfill(2)}"] = float(
                    r["close"].replace("$", "").replace(",", "").strip())
        except Exception as e:        # noqa: BLE001
            log(f"nasdaq {s} failed: {repr(e)[:120]}")
        if series:
            out[s] = series
        else:
            rt = nasdaq_realtime(s)
            if rt:
                out[s] = rt
                log(f"nasdaq {s}: historical empty -> realtime fallback "
                    f"{list(rt.items())[0]}")
        time.sleep(1.2)
    return out


def yahoo_closes(syms: list, rng: str = "3mo") -> dict:
    """Fallback: {sym: {iso_date: close}} from the Yahoo chart API."""
    out = {}
    for s in syms:
        for host in ("query1", "query2"):
            try:
                txt = http(f"https://{host}.finance.yahoo.com/v8/finance/chart/{s}"
                           f"?range={rng}&interval=1d", tries=1)
                res = json.loads(txt)["chart"]["result"][0]
                ts, q = res["timestamp"], res["indicators"]["quote"][0]
                series = {iso(ts[i]): float(q["close"][i])
                          for i in range(len(ts)) if q["close"][i] is not None}
                if series:
                    out[s] = series
                    break
            except Exception:         # noqa: BLE001
                time.sleep(6)
        time.sleep(2)
    return out


# ----------------------------------------------------------------- evaluation

def parse_dividend_rows(rows: list, upto: str = None) -> list:
    """Pure parse of Nasdaq dividend rows -> [(iso_ex_date, amount)].

    Window is (baseline, upto]: ex-dates STRICTLY AFTER the baseline close (QQQ
    went ex on 2026-09-21, the baseline day itself, and that cash went to the
    previous holder — crediting it would overstate the equity leg) and NOT after
    `upto`. The upper bound matters because the feed also lists *declared future*
    ex-dates: without it, an upcoming dividend is treated as a missing past one
    and raises a false "total return understated" alarm every run.
    """
    out = []
    for r in rows or []:
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(r.get("exOrEffDate") or ""))
        if not m:
            continue
        ex = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
        if ex <= TWEET_DATE:
            continue
        if upto and ex > upto:
            continue
        try:
            amt = float(str(r.get("amount")).replace("$", "").replace(",", ""))
        except ValueError:
            continue
        out.append((ex, amt))
    return sorted(out)


def nasdaq_dividends(sym: str, upto: str = None) -> list:
    """[(iso_ex_date, amount)] for dividends actually paid since the baseline."""
    cls = NASDAQ_CLASS.get(sym, "stocks")
    d = json.loads(http(f"https://api.nasdaq.com/api/quote/{sym}/dividends"
                        f"?assetclass={cls}", headers=NASDAQ_HEADERS))
    rows = ((d.get("data") or {}).get("dividends") or {}).get("rows") or []
    return parse_dividend_rows(rows, upto)


def tr_factor(sym: str, divs: list, series: dict, problems: list) -> float:
    """Compounding dividend-reinvestment factor (total return vs price return).

    Each dividend is reinvested at that ex-date's close: factor *= (1 + d/px).
    A missing price is recorded rather than silently dropped — an unstated
    omission here would quietly understate the equity side.
    """
    factor = 1.0
    dates = sorted(series)
    for ex, amt in divs:
        px = series.get(ex)
        if px is None:
            limit = (dt.date.fromisoformat(ex) + dt.timedelta(days=7)).isoformat()
            cand = [d for d in dates if ex <= d <= limit]
            px = series[cand[0]] if cand else None
        if px:
            factor *= (1.0 + amt / px)
        else:
            problems.append(f"{sym}: no close for {ex} dividend ${amt:.4f} — "
                            f"total return slightly understated")
    return factor


def equity_rets(series: dict, divs_map: dict = None, problems: list = None) -> dict:
    """{sym: (last_date, close, total_return_pct, price_return_pct)}

    Total return is the PRIMARY figure (Rune, 2026-09-26: "total return is the
    fairer comparison") — QQQ alone pays ~0.42%/yr, so price-only quietly
    penalises the equity side of the bet. Price return is kept as the fourth
    element so the report can quote both.
    """
    out = {}
    probs = problems if problems is not None else []
    for s, ser in series.items():
        if s not in BASE or not ser:
            continue
        last = max(ser)
        close = ser[last]
        price_ret = (close / BASE[s] - 1.0) * 100.0
        f = tr_factor(s, (divs_map or {}).get(s, []), ser, probs)
        tr_ret = (close * f / BASE[s] - 1.0) * 100.0
        out[s] = (last, close, tr_ret, price_ret)
    return out


def basket_ret(rets: dict, syms: list):
    """Equal-weight compounded return across `syms`; None if any is missing."""
    vals = [rets[s][2] for s in syms if s in rets]
    if len(vals) != len(syms):
        return None, vals
    acc = 1.0
    for v in vals:
        acc *= (1.0 + v / 100.0)
    return ((acc ** (1.0 / len(vals))) - 1.0) * 100.0, vals


def leg_verdict(qqq_ret: float, ai_ret, btc_ret: float) -> str:
    """Jason needs BOTH QQQ and the AI basket to beat BTC to win a leg."""
    if ai_ret is None:
        return "undecided (AI basket data missing)"
    q, a = qqq_ret > btc_ret, ai_ret > btc_ret
    if q and a:
        return "JASON WINS this leg"
    if not q and not a:
        return "JASON LOSES this leg"
    return f"SPLIT (QQQ {'beats' if q else 'trails'} BTC, AI basket {'beats' if a else 'trails'} BTC)"


def ai_parts_detail(snap) -> list:
    """['NVDA -1.02%', ...] in basket order for the report's detail line."""
    out = []
    for s in AI_BASKET:
        r = snap["equities"].get(s)
        if r:
            out.append(f"{s} {r[2]:+.2f}%")
    return out


def days_between(a: dt.date, b: dt.date) -> int:
    return (b - a).days


def build_snapshot(today_d: dt.date, long_window_from: dt.date = None,
                   want_divs: bool = True):
    """Fetch everything the report needs; returns a dict or raises."""
    syms = [QQQ, MAGS, SPY] + AI_BASKET
    problems = []
    # With dividends in play the window must reach back to the claim baseline,
    # otherwise an ex-date close needed for reinvestment is simply absent.
    if long_window_from:
        frm = long_window_from
    elif want_divs:
        frm = dt.date.fromisoformat(TWEET_DATE)
    else:
        frm = today_d - dt.timedelta(days=45)
    series = nasdaq_closes(syms, frm, today_d)
    if len(series) < len(syms):
        missing = [s for s in syms if s not in series]
        log(f"nasdaq incomplete ({','.join(missing)}), trying yahoo fallback")
        fallback = yahoo_closes(missing, rng="1y" if long_window_from else "3mo")
        series.update(fallback)
    divs_map = {}
    if want_divs:
        for s in syms:
            try:
                divs_map[s] = nasdaq_dividends(s, today_d.isoformat())
            except Exception as e:        # noqa: BLE001
                problems.append(f"{s}: dividend history unavailable "
                                f"({repr(e)[:60]}) — its return is price-only")
                log(f"dividends {s} failed: {repr(e)[:120]}")
            time.sleep(0.6)
    rets = equity_rets(series, divs_map, problems)
    ai_ret, ai_parts = basket_ret(rets, AI_BASKET)

    # BTC: gather every source tolerantly. Kraken is the durable one; CoinGecko
    # is preferred for spot when it answers. A single-source 429 must not kill
    # the run — BTC data is the whole point of this monitor.
    highs, kraken_last = {}, None
    try:
        kraken = btc_highs_kraken()
        highs.update(kraken)
        if kraken:
            kraken_last = kraken[max(kraken)]
    except Exception as e:            # noqa: BLE001
        problems.append(f"kraken: {repr(e)[:80]}")
    spot = None
    try:
        spot = btc_spot()
    except Exception as e:            # noqa: BLE001
        problems.append(f"coingecko spot: {repr(e)[:80]}")
    time.sleep(2)
    try:
        for d, h in btc_highs_coingecko(7).items():
            highs[d] = max(highs.get(d, 0.0), h)
    except Exception as e:            # noqa: BLE001
        problems.append(f"coingecko ohlc: {repr(e)[:80]}")
    if spot is None:
        if kraken_last is None:
            raise RuntimeError("no BTC price available: " + "; ".join(problems))
        spot, problems = kraken_last, problems + ["spot fell back to kraken close"]
    for p in problems:
        log(f"btc source issue: {p}")

    post = {d: h for d, h in highs.items() if d > TWEET_DATE and d <= today_d.isoformat()}
    peak = max(post.items(), key=lambda kv: kv[1]) if post else (None, None)
    if peak[1] is None:
        problems.append("no BTC high data — peak since the post is unknown this run")
        log("btc source issue: no high data (spot is fine, peak unavailable)")
    btc_ret = (spot / BASE_BTC - 1.0) * 100.0

    # Lows (claim C, 2026-10-27): the downside trigger. Kraken is the durable
    # window; CoinGecko 4h is the recent cross-check. Same unit-sanity as highs.
    lows = {}
    try:
        lows.update(btc_lows_kraken())
    except Exception as e:            # noqa: BLE001
        problems.append(f"kraken lows: {repr(e)[:80]}")
    try:
        for d, lo in btc_lows_coingecko(7).items():
            lows[d] = min(lows.get(d, lo), lo)
    except Exception as e:            # noqa: BLE001
        problems.append(f"coingecko lows: {repr(e)[:80]}")

    # Claim C — lowest print since the second tweet, and whether $57k was tested.
    post_lows = {d: lo for d, lo in lows.items()
                 if d > TWEET2_DATE and d <= today_d.isoformat()}
    lowest = min(post_lows.items(), key=lambda kv: kv[1]) if post_lows else (None, None)
    tested_57k = bool(lowest[1] is not None and lowest[1] <= CLAIM_C_57K)

    # Claim D — has the ATH broken since the SECOND tweet (a separate window)?
    post2 = {d: h for d, h in highs.items()
             if d > TWEET2_DATE and d <= today_d.isoformat()}
    peak2 = max(post2.items(), key=lambda kv: kv[1]) if post2 else (None, None)
    ath_broken_since2 = bool(peak2[1] and peak2[1] > BTC_ATH)

    return {
        "as_of": today_d.isoformat(),
        "btc_spot": spot,
        "btc_ret": btc_ret,
        "btc_peak_since": {"date": peak[0], "high": peak[1], "source": "kraken+coingecko"},
        "ath_broken": bool(peak[1] and peak[1] > BTC_ATH),
        "btc_peak_since2": {"date": peak2[0], "high": peak2[1], "source": "kraken+coingecko"},
        "ath_broken_since2": ath_broken_since2,
        "btc_low_since": {"date": lowest[0], "low": lowest[1], "source": "kraken+coingecko"},
        "tested_57k": tested_57k,
        "btc_problems": problems,
        "equities": rets,
        "divs": divs_map,
        "total_return": bool(divs_map),
        "ai_ret": ai_ret,
        "ai_parts": ai_parts,
        "series": series,
    }


def ret_at(snap: dict, sym: str, upto: str):
    """Total return for `sym` valued at the last close on/before `upto`."""
    ser = snap["series"].get(sym, {})
    p = sorted(d for d in ser if d <= upto)
    if not p or sym not in BASE:
        return None
    asof = p[-1]
    divs = [(ex, amt) for ex, amt in (snap.get("divs") or {}).get(sym, [])
            if ex <= asof]
    f = tr_factor(sym, divs, ser, [])
    return (ser[asof] * f / BASE[sym] - 1.0) * 100.0


def milestone_positions(snap: dict, today_d: dt.date) -> list:
    """Per-leg status: open / due / decided, with returns at the milestone date."""
    rows = []
    for label, mdate in MILESTONES:
        md = dt.date.fromisoformat(mdate)
        row = {"leg": label, "date": mdate, "days_to": days_between(today_d, md)}
        if today_d < md:
            row["status"] = "open"
            rows.append(row)
            continue
        # milestone reached: value everything at the last date on/before it,
        # with dividends reinvested up to that date (total return).
        qs = snap["series"].get(QQQ, {})
        prev = sorted(d for d in qs if d <= mdate)
        if not prev:
            row["status"] = "due-needs-longer-window"
            rows.append(row)
            continue
        asof = prev[-1]
        qqq_ret = ret_at(snap, QQQ, mdate)
        parts, vals = [], []
        for s in AI_BASKET:
            v = ret_at(snap, s, mdate)
            if v is not None:
                vals.append(v)
        if vals:
            acc = 1.0
            for v in vals:
                acc *= (1.0 + v / 100.0)
            ai_ret = ((acc ** (1.0 / len(vals))) - 1.0) * 100.0
            parts = vals
        else:
            ai_ret = None
        bser = btc_daily(max(30, min(360, days_between(dt.date.fromisoformat(TWEET_DATE), md) + 10)))
        bdates = sorted(d for d in bser if d <= mdate)
        btc_ret = (bser[bdates[-1]] / BASE_BTC - 1.0) * 100.0 if bdates else None
        if btc_ret is None:
            row["status"] = "due-data-missing"
            rows.append(row)
            continue
        row.update({
            "status": "decided", "asof": asof, "qqq_ret": qqq_ret,
            "ai_ret": ai_ret, "btc_ret": btc_ret,
            "verdict": leg_verdict(qqq_ret, ai_ret, btc_ret),
        })
        rows.append(row)
    return rows


def bets(snap: dict, legs: list, today_d: dt.date) -> dict:
    """Rune: each checkable prediction is ONE bet — SIX across Jason's two tweets.

      * 12 / 24 / 36 months (2026-09-21 tweet) — each a bet that BOTH $QQQ and the
        top-AI basket beat $BTC by that mark (settles 2027/2028/2029-09-21).
      * "no new BTC record high in 12 months" (2026-09-21) — lost early by BTC
        actually breaking it, won when the 12 months pass unbroken.
      * "test the 57k low again in 12 months" (2026-10-27) — won early by BTC
        printing at/below $57,000, lost when the 12 months pass without it.
      * "never break ATH again" (2026-10-27) — permanent, lost early by any break
        after that tweet, never "won" in finite time (only "still holding").

    Returns counts plus one row per bet, so every artefact can say "N of 6".
    """
    rows = []
    by_leg = {r["leg"]: r for r in legs}
    qqq_ret = snap["equities"].get(QQQ, (None, None, None))[2]
    ai_ret = snap["ai_ret"]
    btc_ret = snap["btc_ret"]
    gq = None if qqq_ret is None else qqq_ret - btc_ret
    ga = None if ai_ret is None else ai_ret - btc_ret
    if gq is None or ga is None:
        standing = "no close data yet"
    elif gq > 0 and ga > 0:
        standing = "both ahead of BTC"
    else:
        # name the leg that is actually holding the bet back (the worse of the two)
        worse, wn = (ga, "the AI basket") if (ga or 9e9) < (gq or 9e9) else (gq, "QQQ")
        standing = f"{wn} {abs(worse):.2f}% behind BTC"

    for label, mdate in MILESTONES:
        r = by_leg.get(label) or {}
        status, note = "open", standing
        if r.get("status") == "decided":
            v = r.get("verdict") or ""
            status = "won" if "WINS" in v else "lost"
            note = v
        rows.append({"name": f"{label}-month call", "kind": "horizon",
                     "resolves": mdate, "status": status, "note": note,
                     "leg": label})

    ath = BTC_ATH
    if snap.get("ath_broken"):
        pk = snap.get("btc_peak_since") or {}
        status = "lost"
        hi = pk.get("high")
        note = (f"BTC broke ${ath:,.0f} — high ${hi:,.0f} on {pk.get('date')}"
                if hi else f"BTC broke ${ath:,.0f}")
    elif today_d > dt.date.fromisoformat(WINDOW_END_B):
        status = "won"
        note = "12 months passed without a new record high"
    else:
        status = "open"
        below = abs(snap["btc_spot"] / ath - 1.0) * 100.0
        note = f"BTC {below:.1f}% below ${ath:,.0f} · {(dt.date.fromisoformat(WINDOW_END_B) - today_d).days} days left"
    rows.append({"name": "no new BTC record high", "kind": "ath",
                 "resolves": WINDOW_END_B, "status": status, "note": note,
                 "leg": None})

    # Claim C — "test the 57k low again in 12 months" (2026-10-27 tweet).
    # Jason WINS if BTC prints at/below $57,000 within the window; LOSES when
    # 12 months pass without it.
    low = snap.get("btc_low_since") or {}
    if snap.get("tested_57k"):
        status = "won"
        note = (f"BTC printed ${low.get('low'):,.0f} on {low.get('date')} — "
                f"at/below ${CLAIM_C_57K:,.0f}" if low.get("low") is not None
                else "BTC printed at/below $57,000")
    elif today_d > dt.date.fromisoformat(WINDOW_END_C):
        status = "lost"
        note = "12 months passed without BTC printing $57,000"
    else:
        status = "open"
        days_c = (dt.date.fromisoformat(WINDOW_END_C) - today_d).days
        spot_c = snap.get("btc_spot")
        above = (spot_c / CLAIM_C_57K - 1.0) * 100.0 if spot_c else None
        note = (f"BTC {above:.0f}% above ${CLAIM_C_57K:,.0f} · {days_c} days left"
                if above is not None else f"{days_c} days left")
    rows.append({"name": "test $57k low again", "kind": "57k",
                 "resolves": WINDOW_END_C, "status": status, "note": note,
                 "leg": None})

    # Claim D — "Bitcoin will never break its all-time high again" (2026-10-27).
    # Permanent: lost the moment BTC breaks $126,080 after that tweet; never
    # "won" in finite time, only "still holding".
    if snap.get("ath_broken_since2"):
        pk2 = snap.get("btc_peak_since2") or {}
        status = "lost"
        hi = pk2.get("high")
        note = (f"BTC broke ${BTC_ATH:,.0f} — high ${hi:,.0f} on {pk2.get('date')}"
                if hi else f"BTC broke ${BTC_ATH:,.0f}")
    else:
        status = "open"
        note = f"holds while BTC stays below ${BTC_ATH:,.0f} — no expiry"
    rows.append({"name": "never break ATH again", "kind": "never_ath",
                 "resolves": "never", "status": status, "note": note,
                 "leg": None})

    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in ("won", "lost", "open")}
    counts["total"] = len(rows)
    return {"counts": counts, "rows": rows,
            "headline": (f"{counts['won']} of {counts['total']} won · "
                         f"{counts['lost']} lost · {counts['open']} open")}


# ----------------------------------------------------------------- rendering

def _f(v, fmt="{:.2f}", dash="n/a"):
    return dash if v is None else fmt.format(v)


def price_ret_of(r):
    """Price-only return from an equity_rets tuple (4th element), defensively."""
    return r[3] if r and len(r) > 3 else (r[2] if r else None)


def gap_phrase(asset_ret: float, btc_ret: float) -> str:
    """Describe an asset's gap to BTC so it can never be read as BTC's own return.

    The old wording ("vs BTC -4.47%") reversed on the reader — it looked like BTC
    was down 4.47% when it was the AI basket trailing by that many points
    (Rune, 2026-09-26). Always name the direction and the unit.
    """
    gap = asset_ret - btc_ret
    if abs(gap) < 0.005:
        return "level with BTC"
    return f"**{abs(gap):.2f}% {'ahead of' if gap > 0 else 'behind'} BTC**"


def _quote(text: str) -> list:
    """Render a block of plain text as one email quote box (X-style draft)."""
    return ["> " + ln if ln.strip() else ">" for ln in text.strip().split("\n")]


def debasement_lines(snap, infl, supply) -> list:
    """The purchasing-power section — answers Rune's debasement question with numbers."""
    L = ["## Real terms — what's left after the dollar shrinks"]
    if not infl or infl.get("error") or "cum_pct" not in infl:
        L.append("- CPI unavailable this run, so the real-terms block is skipped. The "
                 "nominal figures above are unaffected; it retries next month.")
        return L
    cum = infl["cum_pct"]
    yoy = infl.get("yoy_pct")
    cum_meaningful = abs(cum) >= 0.05
    btc = snap["btc_ret"]
    qqq = snap["equities"].get(QQQ)
    ai = snap["ai_ret"]
    L.append(f"- **US CPI:** {infl['base_month']} ({infl['base_value']:.1f}) → "
             f"{infl['latest_month']} ({infl['latest_value']:.1f}) = **{cum:+.2f}%** "
             f"cumulative · trailing 12-month inflation "
             f"**{_f(yoy)}%/yr** *({infl['source']}; prints land mid-month, so this "
             f"always lags spot by a few weeks).*")
    if not cum_meaningful:
        L.append(f"- **No new CPI print has landed since the claim**, so the "
                 f"cumulative real-terms adjustment is zero *by construction* — not a "
                 f"finding. The live debasement figure is the 12-month rate above: at "
                 f"**{_f(yoy)}%/yr**, a flat nominal BTC loses that much purchasing "
                 f"power per year.")
    for name, nom in ([("BTC", btc)] + ([("QQQ", qqq[2])] if qqq else [])
                      + ([("Top AI names", ai)] if ai is not None else [])):
        suffix = "" if cum_meaningful else " *(= nominal: no new CPI print yet)*"
        L.append(f"- **{name}:** nominal {pct(nom)} → real "
                 f"**{pct(real_return(nom, cum))}**{suffix}")
    if qqq:
        nom_gap = qqq[2] - btc
        real_gap = real_return(qqq[2], cum) - real_return(btc, cum)
        L.append(f"- **The answer to your question:** debasement lifts *both* sides, "
                 f"because both are priced in the same dollars. The QQQ-to-BTC gap "
                 f"scales from **{abs(nom_gap):.2f}%** nominal to "
                 f"**{abs(real_gap):.2f}%** in real terms — it shrinks slightly but "
                 f"**cannot change sign**, since inflation cancels out of a ratio "
                 f"between two dollar-priced assets. So it raises the bar for what "
                 f"\"sideways\" means; it does not give BTC a head start in *this* bet.")
        if cum_meaningful:
            L.append(f"- What it does change is the reading of the *level*: BTC needs "
                     f"**{cum:+.2f}%** just to hold its purchasing power since the "
                     f"claim, and a flat nominal BTC across the whole 12 months would "
                     f"still be a real-terms loss of roughly the window's cumulative "
                     f"inflation.")
        else:
            L.append(f"- What it does change is the reading of the *level*: BTC rising "
                     f"less than ~{_f(yoy)}%/yr means losing purchasing power even while "
                     f"the nominal price goes up. \"Sideways\" is a real-terms decline.")
    if supply:
        L.append(f"- **Where a real asymmetry does exist (separate from the unit):** "
                 f"BTC's supply is fixed — {supply['annual_issuance']:,.0f} new coins/yr "
                 f"on {supply['circulating']:,.0f} = **{supply['annual_pct']:.2f}%/yr**, "
                 f"halving in {supply['halving_year']}. An equity index's share count "
                 f"drifts with net issuance vs buybacks, so the *supply* side of the "
                 f"ratio is structurally kinder to BTC. I could not source a clean "
                 f"index-wide dilution figure, so there is deliberately no number for "
                 f"it here rather than a guessed one.")
    L.append("- The bet as written is in market prices, so **verdicts stay nominal** — "
             "real terms are context, never a scoreboard change.")
    return L


def render_tweet_draft(snap, legs, today_d, infl) -> tuple:
    """A ready-to-post reply to his tweet: (short <= 280 chars, longer version)."""
    day_n = days_between(dt.date.fromisoformat(TWEET_DATE), today_d)
    spot = snap["btc_spot"]
    btc = snap["btc_ret"]
    qqq = snap["equities"].get(QQQ)
    ai = snap["ai_ret"]
    peak = snap["btc_peak_since"]
    below = (spot / BTC_ATH - 1.0) * 100.0
    above_57 = (spot / CLAIM_C_57K - 1.0) * 100.0
    days_left = days_between(today_d, dt.date.fromisoformat(WINDOW_END_B))
    decided = [r for r in legs if r["status"] == "decided"]

    # ---- what the scoreboard says, in one clause each
    score = (f"$BTC {btc:+.1f}%" + (f", $QQQ {qqq[2]:+.1f}%" if qqq else "")
             + (f", top AI names {ai:+.1f}%" if ai is not None else ""))

    # ---- the running score: how many of his 6 bets are settled
    _c = bets(snap, legs, today_d)["counts"]
    if _c["won"] == 0 and _c["lost"] == 0:
        bscore = f"Score: 0 of {_c['total']} settled — all still open."
    elif _c["open"] == 0:
        bscore = f"Final: {_c['won']} of {_c['total']} won."
    else:
        bscore = (f"Score: {_c['won']} won, {_c['lost']} lost, "
                  f"{_c['open']} open.")

    if snap["ath_broken"]:
        lead = (f"Day {day_n}: $BTC printed ${peak['high']:,.0f} on {peak['date']} — "
                f"above the ${BTC_ATH:,.0f} high you said would not break.")
        if today_d > dt.date.fromisoformat(WINDOW_END_B):
            tail = (f"Your 'never break ATH again' call is now lost (the 12-month one "
                    f"had already settled). {bscore}")
        else:
            tail = (f"Both record-high calls — 12-month and 'never again' — are settled "
                    f"lost. {bscore}")
    elif decided:
        r = decided[0]
        lead = (f"{r['leg']} months up: $QQQ {r['qqq_ret']:+.1f}% vs "
                f"{('top AI ' + format(r['ai_ret'], '+.1f') + '%') if r['ai_ret'] is not None else 'AI n/a'} "
                f"vs $BTC {r['btc_ret']:+.1f}%. ")
        lead += ("You win that leg." if "WINS" in r["verdict"] else
                 "You lose that leg." if "LOSES" in r["verdict"] else
                 "That leg is a split.")
        rest = [x for x in (m for _, m in MILESTONES) if x != r["date"]]
        tail = f"Still open: {', '.join(rest)}. {bscore}"
    else:
        # Tight and precise by request (Rune, 2026-09-26): facts only, no filler.
        lead = f"Day {day_n} of 1095. Since your {TWEET_DATE} close: {score}."
        tail = (f"{bscore} BTC {abs(below):.0f}% below ${BTC_ATH:,.0f} and "
                f"{above_57:.0f}% above $57,000.")

    short = f"{lead}\n{tail}"
    if len(short) > 280:               # keep the cap honest, drop the softer clause
        short = lead[:277].rsplit(" ", 1)[0] + "..."

    long_lines = [f"Day {day_n} of 1095 on your call, tracked in the open.",
                  f"Since the {TWEET_DATE} close: {score}.",
                  (f"Your record-high call: $BTC is {abs(below):.1f}% below "
                   f"${BTC_ATH:,.0f} (highest print since your tweet: ${peak['high']:,.0f})."
                   if peak["high"] else
                   f"Your record-high call: $BTC is {abs(below):.1f}% below "
                   f"${BTC_ATH:,.0f}.")]
    long_lines.append(
        f"The running score: {bets(snap, legs, today_d)['headline']}.")
    long_lines.append(
        f"On your {TWEET2_DATE} tweet: BTC is {above_57:.0f}% above the $57,000 you "
        f"said it would test within 12 months, and {abs(below):.0f}% below the record "
        f"high it will 'never' break again.")

    if infl and infl.get("yoy_pct") is not None:
        long_lines.append(
            f"The dollar is losing about {infl['yoy_pct']:.1f}%/yr in purchasing power "
            f"(CPI). That lifts both sides of this comparison equally, so it cannot flip "
            f"the result — but it does mean \"flat\" in nominal terms is a real decline.")
    long_lines.append(f"The marks that settle it: {', '.join(m for _, m in MILESTONES)}.")
    long_lines.append("Not financial advice, and not a prediction of my own.")
    long = "\n".join(long_lines)
    return short, long


def render_monthly(snap, legs, today_d, st, infl=None, supply=None) -> str:
    day_n = days_between(dt.date.fromisoformat(TWEET_DATE), today_d)
    qqq = snap["equities"].get(QQQ)
    mags = snap["equities"].get(MAGS)
    ai = snap["ai_ret"]
    btc = snap["btc_ret"]
    spot = snap["btc_spot"]
    peak = snap["btc_peak_since"]
    need = (BTC_ATH / spot - 1.0) * 100.0
    below = (spot / BTC_ATH - 1.0) * 100.0
    both_behind = bool(qqq and qqq[2] < btc and ai is not None and ai < btc)
    both_ahead = bool(qqq and qqq[2] > btc and ai is not None and ai > btc)

    L = []
    L.append(f"Subject: Jason claim — monthly standing (day {day_n} of 1095)")
    L.append("Kind: jason-claim-standing")
    L.append(f"# Jason's tweet claim — where it stands")
    L.append(f"**Report date:** {today_d.isoformat()} · **claim posted:** "
             f"{TWEET_DATE} (day {day_n} of 1095) · [original tweet]({TWEET_URL})")
    L.append(f"> The claim: *\"90% chance that $QQQ and top AI companies will beat "
             f"$BTC over the next 12, 24 and 36 months\"* and *\"Bitcoin won't break "
             f"its record high in next 12 months.\"*")
    L.append(f"> **Second tweet ({TWEET2_DATE}):** *\"We will test the 57k low again "
             f"in the next 12 months\"* and *\"Bitcoin will never break its all-time "
             f"high again\"* — [tweet]({TWEET2_URL}). Jason's bearish track record: "
             f"[\"11 years, still waiting for zero\"]({REF_URL}).")
    L.append("---")
    L.append("## Scoreboard since the tweet")
    L.append(f"- **BTC:** ${spot:,.0f} — **{pct(btc)}** since his post "
             f"(from ${BASE_BTC:,.0f})")
    if qqq:
        L.append(f"- **QQQ:** ${qqq[1]:,.2f} — **{pct(qqq[2])} total return** "
                 f"({pct(price_ret_of(qqq))} price-only) · "
                 f"{gap_phrase(qqq[2], btc)}")
    if ai is not None:
        L.append(f"- **Top AI companies (equal-weight {len(AI_BASKET)}, total "
                 f"return):** **{pct(ai)}** · {gap_phrase(ai, btc)}")
    if mags:
        L.append(f"- *Cross-check — MAGS (mcap-weighted Mag-7):* {pct(mags[2])} · "
                 f"{gap_phrase(mags[2], btc)}")
    if both_behind:
        L.append("- **Where his call stands:** currently **losing** — both QQQ and the "
                 "AI basket are behind BTC. It only counts at the 12/24/36-month marks, "
                 "but that is the wrong side of it for him today.")
    elif both_ahead:
        L.append("- **Where his call stands:** currently **winning** — both QQQ and the "
                 "AI basket are ahead of BTC.")
    else:
        L.append("- **Where his call stands:** **mixed** — his claim needs *both* QQQ "
                 "and the AI basket above BTC at each mark.")
    parts = ai_parts_detail(snap)
    if parts:
        L.append(f"- Basket detail: {' · '.join(parts)}")
    L.append("---")
    L.append("## The 6 bets — how many he has won")
    _b = bets(snap, legs, today_d)
    L.append(f"**{_b['headline']}**")
    L.append("")
    L.append("| Bet | Settles | Status | Where it stands |")
    L.append("| --- | --- | --- | --- |")
    for _r in _b["rows"]:
        _ico = {"won": "WON", "lost": "LOST"}.get(_r["status"], "OPEN")
        L.append(f"| {_r['name']} | {_r['resolves']} | **{_ico}** | {_r['note']} |")
    L.append("")
    L.append("Each 12/24/36-month bet needs **both** QQQ and the AI basket above "
             "BTC at that mark. The record-high bet can only be **lost early** — "
             "by BTC actually breaking it — and the 'test $57k' bet can only be "
             "**won early**. The 'never break ATH again' bet has no expiry: it can "
             "only be lost (by a break) or keep holding.")
    L.append("")
    L.append("## Claim B — the record-high call (decides 2027-09-21)")
    if snap["ath_broken"]:
        L.append(f"- ⚠️ **BROKEN.** BTC printed ${peak['high']:,.0f} on {peak['date']} "
                 f"(record high to beat was ${BTC_ATH:,.0f}). This part of his call has already lost.")
    else:
        L.append(f"- **Still intact.** BTC is **{below:+.2f}%** from the record high "
                 f"(${spot:,.0f} vs ${BTC_ATH:,.0f}, set {BTC_ATH_DATE}), i.e. "
                 f"**${BTC_ATH - spot:,.0f} below it**.")
        if peak["high"]:
            L.append(f"- Highest print since the tweet: **${peak['high']:,.0f}** on "
                     f"{peak['date']} — ${BTC_ATH - peak['high']:,.0f} below the record.")
        else:
            L.append("- ⚠️ **No BTC high-price data this run**, so the \"highest print "
                     "since the tweet\" figure could not be checked — the spot distance "
                     "above is still sourced. Retried on the next run.")
        L.append(f"- BTC needs **{need:+.1f}%** to break it; **{days_between(today_d, dt.date.fromisoformat(WINDOW_END_B))} days** left in the window.")
    L.append("---")
    L.append("## Claim C — test $57,000 (decides 2027-10-27)")
    low = snap.get("btc_low_since") or {}
    if snap.get("tested_57k"):
        L.append(f"- ✅ **TESTED.** BTC printed ${low['low']:,.0f} on {low['date']} — "
                 f"at/below ${CLAIM_C_57K:,.0f}. Jason wins this bet.")
    else:
        above = (spot / CLAIM_C_57K - 1.0) * 100.0
        L.append(f"- **Still above.** BTC is **{above:+.1f}%** above ${CLAIM_C_57K:,.0f} "
                 f"(${spot:,.0f} vs $57,000).")
        if low.get("low"):
            L.append(f"- Lowest print since his tweet: **${low['low']:,.0f}** on {low['date']}.")
        L.append(f"- Needs a **${spot - CLAIM_C_57K:,.0f}** drop to test it; "
                 f"**{days_between(today_d, dt.date.fromisoformat(WINDOW_END_C))} days** left.")
    L.append("---")
    L.append("## Claim D — never break the all-time high again (no expiry)")
    if snap.get("ath_broken_since2"):
        pk2 = snap.get("btc_peak_since2") or {}
        L.append(f"- ⚠️ **BROKEN.** BTC printed ${pk2['high']:,.0f} on {pk2['date']} — "
                 f"above ${BTC_ATH:,.0f}. Jason loses this bet too.")
    else:
        L.append(f"- **Holding.** BTC is **{below:+.2f}%** below ${BTC_ATH:,.0f}. This "
                 f"one has no end date — it is won only in the sense that it keeps "
                 f"holding.")
    L.append("---")
    L.append("## Claim A — QQQ + AI vs BTC, per horizon")
    for r in legs:
        if r["status"] == "open":
            running = f"QQQ {pct(snap['equities'][QQQ][2])}" if QQQ in snap["equities"] else "QQQ n/a"
            if ai is not None:
                running += f" · AI {pct(ai)}"
            L.append(f"- **{r['leg']}-month leg** (decides {r['date']}): open — "
                     f"{r['days_to']} days to go. Since his post: {running} · BTC {pct(btc)}.")
        elif r["status"] == "decided":
            L.append(f"- **{r['leg']}-month leg** (decided {r['date']}, priced "
                     f"{r['asof']}): QQQ {pct(r['qqq_ret'])} · AI {pct(r['ai_ret']) if r['ai_ret'] is not None else 'n/a'} "
                     f"· BTC {pct(r['btc_ret'])} → **{r['verdict']}**")
        else:
            L.append(f"- **{r['leg']}-month leg** (was due {r['date']}): "
                     f"{r['status']} — flagged for a manual look.")
    L.append("---")
    if infl or supply:
        L.extend(debasement_lines(snap, infl, supply))
        L.append("---")
    short, long_ = render_tweet_draft(snap, legs, today_d, infl)
    L.append("## Ready-to-post reply to his tweet")
    L.append(f"Paste-ready. Short version is **{len(short)} characters** (fits the "
             f"280-char cap on the account as it stands); the longer one is for when "
             f"you want to say more. Both are deliberately flat in tone and claim "
             f"nothing before its mark.")
    L.append("**Short:**")
    L.extend(_quote(short))
    L.append("**Longer:**")
    L.extend(_quote(long_))
    L.append("---")
    L.append("## What to watch next")
    L.append(f"- **Two live triggers, both checked daily:** a print above **${BTC_ATH:,.0f}** "
             f"kills claim B (and claim D) · a print at/below **${CLAIM_C_57K:,.0f}** wins "
             f"claim C. Either one emails you the same day.")
    L.append(f"- Next scheduled report: **1st of next month**.")
    L.append(f"- Nothing here needs your action unless BTC breaks ${BTC_ATH:,.0f} or prints "
             f"${CLAIM_C_57K:,.0f}.")
    L.append("")
    probs = snap.get("btc_problems") or []
    if probs:
        L.append(f"*Source note: one or more BTC feeds were unavailable this run — "
                 f"{'; '.join(probs[:3])}. Figures above use what did answer; the "
                 f"next run re-checks.*")
    if qqq:
        L.append(f"*Return convention: equity legs are **total return** — every "
                 f"dividend after {TWEET_DATE} is reinvested at its ex-date close "
                 f"(source: Nasdaq's dividend record). BTC pays no distributions, so "
                 f"it is unaffected. On a price-only basis QQQ is "
                 f"{pct(price_ret_of(qqq))}.*")
    L.append(f"*Baselines frozen at the {TWEET_DATE} close: QQQ {BASE['QQQ']:.2f}, "
             f"BTC ${BASE_BTC:,.2f} (Nasdaq historical / CoinGecko). Record high "
             f"${BTC_ATH:,.0f} (CoinGecko {BTC_ATH_DATE}; Kraken daily high the same "
             f"day ${BTC_ATH_ALT:,.2f}). \"Top AI companies\" = equal-weight "
             f"{', '.join(AI_BASKET)} — Alfred's reading of a vague phrase, easy to "
             f"change. SPCX (SpaceX+xAI since the Feb-2026 merger, IPO'd Jun 2026) is "
             f"itself a Nasdaq-100 member, so QQQ already holds it: those two lines "
             f"overlap by design.*")
    return "\n".join(L)


def render_ath_alert(snap, today_d) -> str:
    peak = snap["btc_peak_since"]
    b = bets(snap, milestone_positions(snap, today_d), today_d)
    after_b = today_d > dt.date.fromisoformat(WINDOW_END_B)
    if after_b:
        subject = "⚠️ BTC broke its record high — Jason's 'never break ATH' bet is LOST"
        detail = ("his 12-month record-high call had already settled (won) at "
                  "2027-09-21, so this print ends only the open-ended 'never' claim")
    else:
        subject = "⚠️ BTC broke its record high — 2 of Jason's bets are LOST"
        detail = ("both the 12-month record-high call and the 'never break ATH again' "
                  "call are settled by the same print")
    L = [f"Subject: {subject}",
         "Kind: jason-claim-alert",
         "# Jason's record-high bet(s) just lost",
         f"**BTC printed ${peak['high']:,.0f} on {peak['date']}** — above the record "
         f"high of **${BTC_ATH:,.0f}** ({BTC_ATH_DATE}).",
         f"- {detail}.",
         f"- **Bets now: {b['headline']}**.",
         f"- BTC spot at detection: ${snap['btc_spot']:,.0f} ({pct(snap['btc_ret'])} since the first tweet).",
         f"- [First tweet]({TWEET_URL}) · [\"never break ATH\" tweet]({TWEET2_URL})",
         "> His other predictions (QQQ + top AI beating BTC over 12/24/36 months, and "
         "the $57k call) are separate bets and still measured.",
         ""]
    return "\n".join(L)


def render_57k_alert(snap, today_d) -> str:
    low = snap.get("btc_low_since") or {}
    b = bets(snap, milestone_positions(snap, today_d), today_d)
    L = [f"Subject: ⚠️ BTC printed $57,000 — Jason's 'test the 57k low' bet just WON",
         "Kind: jason-claim-alert",
         "# Jason's 'test $57k' bet just won",
         f"**BTC printed ${low.get('low'):,.0f} on {low.get('date')}** — at/below the "
         f"**${CLAIM_C_57K:,.0f}** level his {TWEET2_DATE} tweet said it would test "
         f"within 12 months.",
         f"- **Bets now: {b['headline']}**.",
         f"- BTC spot at detection: ${snap['btc_spot']:,.0f} ({pct(snap['btc_ret'])} since the first tweet).",
         f"- [Original tweet]({TWEET2_URL})",
         "",
         "*His other predictions (QQQ + top AI, the two record-high calls) are separate "
         "bets and still measured.*",
         ""]
    return "\n".join(L)


def render_milestone_alert(row, today_d) -> str:
    L = [f"Subject: Jason's {row['leg']}-month call is decided — {row['verdict']}",
         "Kind: jason-claim-alert",
         f"# {row['leg']}-month leg decided: {row['verdict']}",
         f"Priced at the {row['asof']} close ({row['date']} was the 12/24/36-month mark).",
         f"- **QQQ:** {pct(row['qqq_ret'])}",
         f"- **Top AI companies (equal weight):** {pct(row['ai_ret']) if row['ai_ret'] is not None else 'n/a'}",
         f"- **BTC:** {pct(row['btc_ret'])}",
         f"- His requirement: both QQQ *and* the AI basket above BTC.",
         f"- [Original tweet]({TWEET_URL})",
         "",
         "*Standing is from the 2026-09-21 close; the monthly report carries the rest.*",
         ""]
    return "\n".join(L)


# ----------------------------------------------------------------- delivery

# Hard guard: when set, NO delivery path may send. The self-test sets this so a
# harness bug can never put mail in a real mailbox (it did once — 2026-09-26, a
# stubbed-numbers copy reached Rune's inbox because the new rich path was not
# intercepted). Defence in depth next to the harness's own monkeypatching.
SEND_GUARD_ENV = "JASON_MONITOR_NO_SEND"


def _send_blocked() -> bool:
    if os.environ.get(SEND_GUARD_ENV):
        log("send blocked by JASON_MONITOR_NO_SEND guard")
        print(f"[BLOCKED] delivery suppressed ({SEND_GUARD_ENV} set)")
        return True
    return False


def build_visuals(snap, legs, today_d, infl, supply, short, long_, dry) -> tuple:
    """Render the dashboard HTML + the 16:9 tweet card PNG.

    Returns (html_path, png_path) — either may be None. Deliberately failure-
    tolerant: if the dashboard module or headless Chrome is unavailable the
    caller falls back to the plain HTML email, because a report that arrives
    is worth more than a pretty one that does not.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("jason_dash", DASH_MODULE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:            # noqa: BLE001
        log(f"dashboard module unavailable: {repr(e)[:160]}")
        return None, None

    REPORTS.mkdir(parents=True, exist_ok=True)
    html_path = REPORTS / f"dashboard-{today_d.isoformat()}.html"
    card_path = REPORTS / f"card-{today_d.isoformat()}.html"
    png_path = REPORTS / f"card-{today_d.isoformat()}.png"
    try:
        b = bets(snap, legs, today_d)
        html_path.write_text(
            mod.dashboard_html(snap, legs, today_d, infl, supply, short, long_,
                               snap.get("btc_problems"), b), encoding="utf-8")
        card_path.write_text(mod.card_html(snap, legs, today_d, b), encoding="utf-8")
        (REPORTS / "latest-dashboard.html").write_text(
            html_path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception as e:            # noqa: BLE001
        log(f"dashboard html failed: {repr(e)[:160]}")
        return None, None

    if os.environ.get("SKIP_PNG"):
        return html_path, None
    try:
        r = subprocess.run(
            [sys.executable, SHOT, str(card_path), str(png_path),
             "--width", "1080", "--height", "1350", "--scale", "2"],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not png_path.exists():
            log(f"png render failed rc={r.returncode}: "
                f"{(r.stdout or r.stderr or '').strip()[:200]}")
            return html_path, None
        log(f"png rendered: {png_path.name} "
            f"({png_path.stat().st_size} bytes)")
    except Exception as e:            # noqa: BLE001
        log(f"png render raised: {repr(e)[:160]}")
        return html_path, None
    return html_path, png_path


def send_rich(md: str, html_path, png_path, name: str, dry: bool) -> bool:
    """Email = plain-text fallback + dashboard HTML + the postable card image."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / name
    path.write_text(md, encoding="utf-8")
    (REPORTS / "latest.md").write_text(md, encoding="utf-8")
    if not dry and _send_blocked():
        return False
    cmd = [sys.executable, RICH_SENDER, "--text", str(path),
           "--html", str(html_path)]
    if png_path:
        cmd += ["--attach", str(png_path)]
    if dry:
        cmd.append("--dry-run")
    env = {**os.environ, "REPORT_TO": RECIPIENTS, "HOME": "/opt/data/home"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    ok = r.returncode == 0
    log(f"send_rich {name}: rc={r.returncode} "
        f"{(r.stdout or r.stderr or '').strip()[:200]}")
    if dry:
        print((r.stdout or "")[:500])
    if not ok:
        print(f"EMAIL FAILED (rich): {(r.stdout or r.stderr or '').strip()[:400]}",
              file=sys.stderr)
    return ok


def send(md: str, name: str, dry: bool) -> bool:
    """Write the rendered mail and hand it to the proven HTML sender."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / name
    path.write_text(md, encoding="utf-8")
    (REPORTS / "latest.md").write_text(md, encoding="utf-8")
    if dry:
        print(f"[DRY-RUN] rendered -> {path}\n{'-' * 72}\n{md}\n{'-' * 72}")
        return True
    if _send_blocked():
        return False
    env = {**os.environ, "REPORT_TO": RECIPIENTS, "HOME": "/opt/data/home"}
    r = subprocess.run([sys.executable, SENDER, str(path)],
                       capture_output=True, text=True, timeout=180, env=env)
    ok = r.returncode == 0
    log(f"send {name}: rc={r.returncode} {(r.stdout or r.stderr or '').strip()[:200]}")
    if not ok:
        print(f"EMAIL FAILED: {(r.stdout or r.stderr or '').strip()[:400]}",
              file=sys.stderr)
    return ok


# ----------------------------------------------------------------- modes

def run_monthly(dry: bool, force: bool) -> int:
    t = today()
    st = load_state()
    key = t.strftime("%Y-%m")
    if st.get("last_monthly") == key and not force:
        log(f"monthly {key}: already sent, silent")
        return 0
    try:
        snap = build_snapshot(t)
        legs = milestone_positions(snap, t)
    except Exception as e:            # noqa: BLE001
        log(f"monthly fetch failed: {repr(e)[:200]}")
        if st.get("failure_email_date") != t.isoformat():
            send(f"Subject: Jason-claim monitor: data source failed\n"
                 f"Kind: jason-claim-alert\n\n"
                 f"# Jason-claim monitor could not fetch data\n\nThe monthly standing "
                 f"report for {t.isoformat()} did not run.\n\nError: `{repr(e)[:200]}`\n\n"
                 f"Next attempt: tomorrow's daily check / next monthly run.\n",
                 f"failure-{t.isoformat()}.md", dry)
            st["failure_email_date"] = t.isoformat()
            if not dry:
                save_state(st)
        return 0
    infl = supply = None
    try:
        infl = inflation_block()
    except Exception as e:            # noqa: BLE001
        log(f"inflation block failed: {repr(e)[:160]}")
    try:
        supply = btc_supply()
    except Exception as e:            # noqa: BLE001
        log(f"supply block failed: {repr(e)[:160]}")
    md = render_monthly(snap, legs, t, st, infl=infl, supply=supply)
    short, long_ = render_tweet_draft(snap, legs, t, infl)
    html_path, png_path = build_visuals(snap, legs, t, infl, supply, short, long_,
                                        dry)
    if html_path:
        ok = send_rich(md, html_path, png_path, f"standing-{t.isoformat()}.md", dry)
    else:
        ok = send(md, f"standing-{t.isoformat()}.md", dry)
    if ok and not dry:
        st["last_monthly"] = key
        st["last_snapshot"] = {k: snap[k] for k in
                              ("as_of", "btc_spot", "btc_ret", "btc_peak_since", "ai_ret")}
        save_state(st)
    print(f"monthly standing rendered ({'dry' if dry else 'sent'}) as of {t}")
    return 0


def run_daily(dry: bool, force: bool) -> int:
    t = today()
    st = load_state()
    if st.get("last_daily") == t.isoformat() and not force:
        return 0
    try:
        snap = build_snapshot(t, want_divs=False)   # ATH watch needs no equity returns
    except Exception as e:            # noqa: BLE001
        log(f"daily fetch failed: {repr(e)[:200]}")
        print(f"daily check failed to fetch: {repr(e)[:160]}")
        return 0
    acted = False

    # 1) claim B — the record high
    if snap["ath_broken"] and not st.get("ath_broken_notified"):
        stake = snap["btc_peak_since"]
        send(render_ath_alert(snap, t), f"ALERT-ath-broken-{t.isoformat()}.md", dry)
        st["ath_broken_notified"] = {"date": stake["date"], "high": stake["high"],
                                     "detected": t.isoformat()}
        acted = True

    # 2) claim C — BTC tested $57,000 (Jason WINS that bet)
    if snap.get("tested_57k") and not st.get("tested_57k_notified"):
        low = snap.get("btc_low_since") or {}
        send(render_57k_alert(snap, t), f"ALERT-57k-tested-{t.isoformat()}.md", dry)
        st["tested_57k_notified"] = {"date": low.get("date"), "low": low.get("low"),
                                     "detected": t.isoformat()}
        acted = True

    # 3) claim A — a milestone that has arrived and is not yet reported
    resolved = st.get("milestones_reported") or {}
    already = {v.get("date") for v in resolved.values()}
    pending = [m for _, m in MILESTONES
               if dt.date.fromisoformat(m) <= t and m not in already]
    if pending:
        first = min(dt.date.fromisoformat(m) for m in pending)
        window_from = first - dt.timedelta(days=5)
        try:
            legs = milestone_positions(build_snapshot(t, long_window_from=window_from), t)
        except Exception as e:        # noqa: BLE001
            log(f"milestone fetch failed: {repr(e)[:160]}")
            legs = []
        for row in legs:
            if row["status"] == "decided" and row["date"] in pending:
                send(render_milestone_alert(row, t),
                     f"ALERT-milestone-{row['leg']}m-{t.isoformat()}.md", dry)
                st.setdefault("milestones_reported", {})[row["leg"]] = {
                    "date": row["date"], "verdict": row["verdict"],
                    "reported": t.isoformat()}
                acted = True

    if not dry:
        st["last_daily"] = t.isoformat()
        save_state(st)
    if not acted:
        log(f"daily {t}: silent (BTC ${snap['btc_spot']:,.0f}, peak since tweet "
            f"${snap['btc_peak_since']['high'] or 0:,.0f})")
    print(f"daily check {t}: {'event(s) sent' if acted else 'silent'} — "
          f"BTC ${snap['btc_spot']:,.0f}, peak since tweet "
          f"${snap['btc_peak_since']['high'] or 0:,.0f} vs record ${BTC_ATH:,.0f}")
    return 0


def run_status() -> int:
    t = today()
    snap = build_snapshot(t)
    legs = milestone_positions(snap, t)
    print(render_monthly(snap, legs, t, load_state()))
    return 0


# ----------------------------------------------------------------- selftest

def selftest() -> int:
    """Stubbed end-to-end test — no network, no mail, throwaway state dir."""
    import tempfile

    global STATE_DIR, STATE, REPORTS, LOG
    tmp = Path(tempfile.mkdtemp(prefix="jason-selftest-"))
    STATE_DIR, STATE, REPORTS, LOG = tmp, tmp / "state.json", tmp / "reports", tmp / "monitor.log"
    fails, sent = [], []
    real_send = globals()["send"]          # kept so the guard can be proven

    def fake_send(md, name, dry):
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / name).write_text(md, encoding="utf-8")
        sent.append((name, md))
        return True

    def fake_rich_send(md, html_path, png_path, name, dry):
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / name).write_text(md, encoding="utf-8")
        sent.append((name, md))
        return True

    globals()["send"] = fake_send
    globals()["send_rich"] = fake_rich_send
    # Arm the delivery guard AND skip the PNG (Chrome) so the harness is offline.
    saved_env = {k: os.environ.get(k) for k in (SEND_GUARD_ENV, "SKIP_PNG")}
    os.environ[SEND_GUARD_ENV] = "1"
    os.environ["SKIP_PNG"] = "1"

    def check(name, cond, extra=""):
        print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"   <- {extra}"))
        if not cond:
            fails.append(name)

    def stub(equity_mult=None, spot_v=84000.0, highs=None, btc_at=None,
             days=None, infl=None, supply=True, divs=None, lows=None):
        globals()["btc_spot"] = lambda: spot_v
        globals()["btc_highs_kraken"] = lambda: dict(highs or {})
        globals()["btc_highs_coingecko"] = lambda days=7: {}
        globals()["btc_lows_kraken"] = lambda: dict(lows or {})
        globals()["btc_lows_coingecko"] = lambda days=7: {}
        globals()["nasdaq_dividends"] = lambda sym, upto=None: [
            (d, a) for d, a in (divs or {}).get(sym, []) if not upto or d <= upto]
        globals()["inflation_block"] = lambda: dict(
            infl or {"base_month": CPI_BASE_MONTH, "latest_month": "2027-08",
                     "base_value": 334.131, "latest_value": 340.0,
                     "cum_pct": (340.0 / 334.131 - 1) * 100,
                     "yoy_pct": 2.9, "source": "STUB FRED",
                     "m2": {"month": "2027-08", "value_bn": 24000.0, "yoy_pct": 3.1}})
        globals()["btc_supply"] = (lambda: {
            "circulating": 19_900_000.0, "annual_issuance": 164_250.0,
            "annual_pct": 164_250.0 / 19_900_000.0 * 100,
            "subsidy": BTC_SUBSIDY, "halving_year": BTC_HALVING_YEAR}) if supply else (lambda: None)
        dates = days or [TWEET_DATE, dt.date.fromisoformat(TWEET_DATE).isoformat()]

        def nas(syms, frm, to):
            out = {}
            for s in syms:
                if s not in BASE:
                    continue
                mult = (equity_mult or {}).get(s, 1.0)
                out[s] = {d: BASE[s] * (1.0 if d == TWEET_DATE else mult)
                          for d in dates}
            return out

        globals()["nasdaq_closes"] = nas
        globals()["yahoo_closes"] = lambda syms, rng="3mo": {}
        globals()["btc_daily"] = lambda d: dict(btc_at or {TWEET_DATE: BASE_BTC,
                                                          dates[-1]: spot_v})

    def set_today(v):
        if v is None:
            os.environ.pop("TODAY", None)
        else:
            os.environ["TODAY"] = v

    t_saved = os.environ.get("TODAY")
    try:
        # 1 — normal day, claim intact, both behind BTC -> "losing" wording
        set_today("2026-09-26")
        STATE.unlink(missing_ok=True)
        stub(equity_mult={s: 0.99 for s in AI_BASKET} | {QQQ: 1.004},
             spot_v=84300.0, highs={"2026-09-25": 87274.4})
        snap = build_snapshot(today())
        md = render_monthly(snap, milestone_positions(snap, today()), today(), {})
        check("intact state detected", snap["ath_broken"] is False)
        check("report says record intact", "Still intact" in md, md[:200])
        check("report says his call is losing", "currently **losing**" in md, md[:400])
        check("gap shown as below, not -49%", "below it**" in md and "-49.5" not in md)
        check("all three legs listed open", md.count("-month leg**") == 3)

        # 2 — monthly send + idempotence (goes down the RICH path: html + card png)
        check("delivery guard is armed in the harness",
              os.environ.get(SEND_GUARD_ENV) == "1")
        n0 = len(sent)
        run_monthly(False, False)
        check("monthly email sent once", len(sent) == n0 + 1,
              sent[-1][0] if sent else "NOTHING RECORDED")
        check("monthly state stamped", load_state().get("last_monthly") == "2026-09")
        run_monthly(False, False)
        check("monthly not resent same month", len(sent) == n0 + 1)
        run_monthly(False, True)   # FORCE
        check("monthly FORCE resends", len(sent) == n0 + 2)

        # 3 — BTC breaks the record high -> alert once, then silent
        n1 = len(sent)
        STATE.unlink(missing_ok=True)
        stub(spot_v=130000.0, highs={"2026-09-25": 130500.0})
        run_daily(False, False)
        check("ATH break alert fired", any("ALERT-ath-broken" in n for n, _ in sent[n1:]),
              [n for n, _ in sent[n1:]])
        check("ATH alert wording", "record-high bet" in sent[-1][1],
              sent[-1][1][:120])
        check("ATH alert carries the gate Kind",
              "Kind: jason-claim-alert" in sent[-1][1], sent[-1][1][:160])
        check("ATH alert carries the bet scoreboard",
              "of 6 won" in sent[-1][1],
              [l for l in sent[-1][1].splitlines() if "Bets now" in l][:1])
        check("ATH break recorded in state",
              bool(load_state().get("ath_broken_notified")))
        n2 = len(sent)
        run_daily(False, False)
        check("ATH alert not repeated", len(sent) == n2)
        check("second daily run still stamps state",
              load_state().get("last_daily") == "2026-09-26")
        run_daily(True, False)     # same day, dry
        check("dry daily run is silent", len(sent) == n2)

        # 3b — BTC tests $57,000 -> claim C won early (Jason wins), alert once
        set_today("2026-11-05")
        STATE.unlink(missing_ok=True)
        stub(spot_v=56900.0, highs={"2026-11-04": 58000.0},
             lows={"2026-11-04": 56800.0})
        n57 = len(sent)
        run_daily(False, False)
        check("57k test alert fired",
              any("ALERT-57k-tested" in n for n, _ in sent[n57:]),
              [n for n, _ in sent[n57:]])
        check("57k alert wording", "test $57k" in sent[-1][1], sent[-1][1][:160])
        check("57k alert carries the gate Kind",
              "Kind: jason-claim-alert" in sent[-1][1], sent[-1][1][:160])
        check("57k alert says Jason wins the bet", "WON" in sent[-1][1],
              sent[-1][1][:160])
        check("57k recorded in state", bool(load_state().get("tested_57k_notified")))
        n57b = len(sent)
        run_daily(False, False)
        check("57k alert not repeated", len(sent) == n57b)

        # 4 — milestone decided: he WINS (both QQQ and AI above BTC)
        set_today("2027-09-22")
        STATE.unlink(missing_ok=True)
        stub(equity_mult={s: 1.80 for s in AI_BASKET} | {QQQ: 1.50},
             spot_v=90000.0, highs={},
             days=[TWEET_DATE, "2027-09-21"],
             btc_at={TWEET_DATE: BASE_BTC, "2027-09-21": 85000.0})
        run_daily(False, False)
        check("milestone alert fired", any("ALERT-milestone-12m" in n for n, _ in sent),
              [n for n, _ in sent][-3:])
        check("milestone verdict = he wins", "JASON WINS" in sent[-1][1], sent[-1][1][:300])
        check("milestone recorded", "12" in (load_state().get("milestones_reported") or {}))

        # 5 — same milestone, split result (QQQ beats BTC, AI basket does not)
        set_today("2027-09-22")
        STATE.unlink(missing_ok=True)
        stub(equity_mult={s: 1.02 for s in AI_BASKET} | {QQQ: 1.50},
             spot_v=90000.0, highs={},
             days=[TWEET_DATE, "2027-09-21"],
             btc_at={TWEET_DATE: BASE_BTC, "2027-09-21": 85000.0})
        run_daily(False, False)
        check("split verdict detected", "SPLIT" in sent[-1][1], sent[-1][1][:300])

        # 6 — data-source failure path (monthly) emails once and does not crash
        set_today("2026-10-01")
        STATE.unlink(missing_ok=True)
        globals()["nasdaq_closes"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        globals()["btc_spot"] = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        n3 = len(sent)
        rc = run_monthly(False, False)
        check("monthly survives a fetch failure", rc == 0)
        check("failure email sent once", len(sent) == n3 + 1 and "failed" in sent[-1][1])
        rc = run_monthly(False, False)
        check("failure email throttled per day", rc == 0 and len(sent) == n3 + 1)

        # 7 — real terms (debasement) + the ready-to-post draft
        set_today("2027-09-22")
        STATE.unlink(missing_ok=True)
        stub(equity_mult={s: 1.30 for s in AI_BASKET} | {QQQ: 1.20},
             spot_v=100000.0, highs={"2027-09-20": 101000.0},
             days=[TWEET_DATE, "2027-09-21", "2027-09-22"],
             btc_at={TWEET_DATE: BASE_BTC, "2027-09-21": 99000.0})
        snap = build_snapshot(today())
        legs = milestone_positions(snap, today())
        infl = inflation_block()
        sup = btc_supply()
        md = render_monthly(snap, legs, today(), {}, infl=infl, supply=sup)
        check("real-terms section rendered",
              "Real terms — what's left after the dollar shrinks" in md)
        # hand-checked Fisher maths: 3.86% nominal over 1.756% inflation -> 2.068% real
        check("Fisher conversion correct",
              abs(real_return(3.86, 1.756) - 2.068) < 0.01,
              real_return(3.86, 1.756))
        check("sign-flip claim stated", "cannot change sign" in md)
        check("supply asymmetry quantified (0.83%/yr)", "0.83%/yr" in md, md)
        check("no guessed dilution number", "no number for" in md)
        check("debasement answer present in email", "answer to your question" in md)
        check("draft section in the email",
              "Ready-to-post reply to his tweet" in md)
        # the zero-new-print case (the live state today) must not read as a finding
        infl_zero = {"base_month": CPI_BASE_MONTH, "latest_month": CPI_BASE_MONTH,
                     "base_value": 334.131, "latest_value": 334.131, "cum_pct": 0.0,
                     "yoy_pct": 3.35, "source": "STUB BLS"}
        md_zero = render_monthly(snap, legs, today(), {}, infl=infl_zero, supply=sup)
        check("zero-new-print case is explained, not presented as a result",
              "No new CPI print has landed" in md_zero and
              "by construction" in md_zero)
        check("zero case still quotes the live 12-month rate",
              "3.35%/yr" in md_zero and "nominal" in md_zero)
        short, long_ = render_tweet_draft(snap, legs, today(), infl)
        check("draft fits the 280-char cap", len(short) <= 280, f"{len(short)} chars")
        check("draft is tight (<= 240 chars, Rune asked for short+precise)",
              len(short) <= 240, f"{len(short)} chars")
        check("no M2 line (Rune: CPI is enough)", "US M2" not in md)
        # regression guard for the ambiguity Rune caught 2026-09-26: "vs BTC -4.47%"
        # read as "BTC is down 4.47%" when it was the AI basket trailing by that gap.
        # Only the signed-gap form is a bug — the section heading "QQQ + AI vs BTC" is fine.
        import re as _re
        amb = _re.findall(r"vs BTC[^\n]{0,8}[-+]\d", md)
        check("no ambiguous 'vs BTC <signed %>' gap form anywhere", not amb, amb[:3])
        check("his exact case now renders unambiguously",
              gap_phrase(-0.51, 3.96) == "**4.47% behind BTC**",
              gap_phrase(-0.51, 3.96))
        check("gap_phrase: ahead", gap_phrase(5.0, 3.96) == "**1.04% ahead of BTC**",
              gap_phrase(5.0, 3.96))
        check("gap_phrase: level", gap_phrase(3.96, 3.96) == "level with BTC",
              gap_phrase(3.96, 3.96))
        # and the fixture's own numbers must produce the matching phrase in the report
        btc_exp = (100000.0 / BASE_BTC - 1) * 100.0
        ai_exp = 30.0
        exp_phrase = (f"**{abs(ai_exp - btc_exp):.2f}% "
                      f"{'ahead of' if ai_exp > btc_exp else 'behind'} BTC**")
        check("report gap text matches the fixture's arithmetic",
              exp_phrase in md, exp_phrase)
        check("draft is 2 lines", short.count("\n") == 1, repr(short))
        # 7c — the visual products render, and agree with the text report
        try:
            import importlib.util as _il
            _sp = _il.spec_from_file_location("jd", DASH_MODULE)
            jd = _il.module_from_spec(_sp)
            _sp.loader.exec_module(jd)
            _bt = bets(snap, legs, today())
            dhtml = jd.dashboard_html(snap, legs, today(), infl, sup, short, long_,
                                      None, _bt)
            chtml = jd.card_html(snap, legs, today(), _bt)
            # the 6-bet scoreboard (3 horizon bets + the two ATH calls + the 57k call)
            check("bets: six of them", _bt["counts"]["total"] == 6,
                  _bt["headline"])
            check("bets: counts add up",
                  sum(_bt["counts"][k] for k in ("won", "lost", "open")) == 6)
            # this section's fixture is dated 2027-09-22, so the 12-month leg is
            # already decided and the 12-month record-high window has closed.
            check("bets: this fixture reads 1 won / 1 lost / 4 open",
                  _bt["counts"] == {"won": 1, "lost": 1, "open": 4, "total": 6},
                  str(_bt["counts"]))
            check("bets: the record-high bet is WON when 12 months pass unbroken",
                  [r for r in _bt["rows"] if r["kind"] == "ath"][0]["status"] == "won")
            check("bets: a SPLIT horizon is a lost bet (his wording is 'and')",
                  [r for r in _bt["rows"]
                   if r.get("leg") == "12"][0]["status"] == "lost")
            # clean pre-milestone fixture, so the counting rules are tested without
            # the 2027 state above leaking in
            _open = [{"leg": l, "date": d, "status": "open", "days_to": 1}
                     for l, d in MILESTONES]
            _bt0 = bets(snap, _open, dt.date(2027, 1, 1))
            check("bets: all six open before any milestone",
                  _bt0["counts"] == {"won": 0, "lost": 0, "open": 6, "total": 6},
                  str(_bt0["counts"]))
            _bt_broken = bets(
                dict(snap, ath_broken=True,
                     btc_peak_since={"high": 130000.0, "date": "2027-01-02"},
                     ath_broken_since2=True,
                     btc_peak_since2={"high": 130000.0, "date": "2027-01-02"}),
                _open, dt.date(2027, 1, 1))
            check("bets: a broken record high loses BOTH ATH bets",
                  _bt_broken["counts"] == {"won": 0, "lost": 2, "open": 4,
                                           "total": 6},
                  str(_bt_broken["counts"]))
            check("bets: the lost record-high bet names the break",
                  all("broke" in r["note"] and "130,000" in r["note"]
                      for r in _bt_broken["rows"] if r["kind"] == "ath"),
                  [r["note"] for r in _bt_broken["rows"] if r["kind"] == "ath"][0])
            check("bets: the 'never break ATH' bet is lost by the same break",
                  [r for r in _bt_broken["rows"] if r["kind"] == "never_ath"][0]
                  ["status"] == "lost")
            check("card carries the scoreboard", _bt["headline"] in chtml,
                  _bt["headline"])
            check("card names the nine AI tickers",
                  "NVDA" in chtml and "SPCX" in chtml)
            check("email carries the bet ledger",
                  _bt["headline"] in dhtml
                  and all(r["name"] in dhtml for r in _bt["rows"]))
            check("plain-text report carries the same bet ledger",
                  "## The 6 bets" in md and _bt["headline"] in md)
            check("monthly report carries the gate Kind",
                  "Kind: jason-claim-standing" in md, md[:120])
            check("no 'pts' gap wording left (Rune asked for %)",
                  all(x not in (dhtml + chtml)
                      for x in ("pts behind", "pts ahead", "pts of")))
            check("dashboard html renders", "JASON" in dhtml and len(dhtml) > 5000,
                  len(dhtml))
            check("card html renders", "JASON" in chtml and len(chtml) > 3000,
                  len(chtml))
            check("both carry the record-high threshold",
                  "126,080" in dhtml and "126,080" in chtml)
            check("card headline matches the snapshot",
                  f"{snap['btc_ret']:+.1f}%" in chtml, f"{snap['btc_ret']:+.1f}%")
            check("dashboard headline matches the snapshot",
                  f"{snap['btc_ret']:+.1f}%" in dhtml, f"{snap['btc_ret']:+.1f}%")
            # phone-first (Rune, 2026-09-26): the wide-screen layout was the problem
            check("card is the 4:5 portrait canvas X supports",
                  'width="1080"' in chtml and 'height="1350"' in chtml)
            check("card has no wide fixed canvas left over",
                  "1120" not in chtml and 'width="1200"' not in chtml)
            check("email is phone-width (600px) with a viewport meta",
                  "max-width:600px" in dhtml and "device-width" in dhtml)
            check("email has no wide table left over",
                  "1120" not in dhtml and 'width="1200"' not in dhtml)
            check("card numbers are large relative to width (>=108px)",
                  "108px" in chtml)

            # dark mode (Rune, 2026-09-26) — assert measured contrast, since a
            # dark card that merely *has* the right colour names can still be
            # unreadable, and the light-theme greens/reds are far too dark here.
            def _lum(hexcol):
                h = hexcol.lstrip("#")
                ch = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
                ch = [(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
                      for c in ch]
                return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

            def _ratio(fg, bg):
                a, b = _lum(fg), _lum(bg)
                hi, lo = max(a, b), min(a, b)
                return (hi + 0.05) / (lo + 0.05)

            check("card is dark mode (no white background left)",
                  jd.DK_BG in chtml and "#ffffff" not in chtml, jd.DK_BG)
            for _name, _fg in (("headline numbers", jd.DK_INK),
                              ("labels", jd.DK_MUTED),
                              ("footnote", jd.DK_DIM),
                              ("positive return", jd.DK_GREEN),
                              ("negative return", jd.DK_RED),
                              ("bar", jd.DK_BLUE)):
                _cr = _ratio(_fg, jd.DK_BG)
                check(f"dark card contrast {_name} >= 4.5:1", _cr >= 4.5,
                      f"{_cr:.1f}:1")
            _light = _ratio(jd.GREEN, jd.DK_BG)
            check("light-theme green would have failed on dark (why we remapped)",
                  _light < 4.5, f"light green on dark = {_light:.1f}:1")
            import re as _re2
            check("card has no signed-gap form",
                  not _re2.search(r"vs BTC[^\n]{0,8}[-+]\d", chtml))
            check("dashboard has no signed-gap form",
                  not _re2.search(r"vs BTC[^\n]{0,8}[-+]\d", dhtml))
        except Exception as e:        # noqa: BLE001
            check("visual module renders", False, repr(e)[:200])
        check("longer draft carries the purchasing-power point",
              "purchasing power" in long_, long_[:200])
        check("longer draft is not a verdict", "Not financial advice" in long_)

        # 7b — the draft changes shape in the three states
        set_today("2026-09-26")          # before any milestone: the intact state
        STATE.unlink(missing_ok=True)
        stub(equity_mult={s: 0.99 for s in AI_BASKET} | {QQQ: 1.004},
             spot_v=84300.0, highs={"2026-09-25": 87274.4})
        snapi = build_snapshot(today())
        si, li = render_tweet_draft(snapi, milestone_positions(snapi, today()),
                                    today(), infl)
        check("intact-state draft fits cap", len(si) <= 280, f"{len(si)} chars")
        check("intact-state draft quotes the threshold",
              f"${BTC_ATH:,.0f}" in si and "$57,000" in si, si)
        check("intact-state draft names both live triggers",
              "below" in si and "$57,000" in si, si)
        check("intact-state draft carries the 6-bet score",
              "Score: 0 of 6 settled" in si and "all still open" in si, si)
        set_today("2027-09-22")
        stub(spot_v=130000.0, highs={"2026-09-25": 130500.0})
        snapb = build_snapshot(today())
        sb, _ = render_tweet_draft(snapb, milestone_positions(snapb, today()),
                                   today(), infl)
        check("broken-record draft fits cap", len(sb) <= 280, f"{len(sb)} chars")
        check("broken-record draft says it is settled", "settled" in sb, sb)
        check("broken-record draft carries the score", "Score:" in sb, sb)
        stub(equity_mult={s: 1.80 for s in AI_BASKET} | {QQQ: 1.50},
             spot_v=90000.0, highs={},
             days=[TWEET_DATE, "2027-09-21"],
             btc_at={TWEET_DATE: BASE_BTC, "2027-09-21": 85000.0})
        snapd = build_snapshot(today())
        sd, _ = render_tweet_draft(snapd, milestone_positions(snapd, today()),
                                   today(), infl)
        check("decided-leg draft fits cap", len(sd) <= 280, f"{len(sd)} chars")
        check("decided-leg draft names the outcome",
              any(w in sd for w in ("win that leg", "lose that leg", "split")), sd)
        check("decided-leg draft carries the score", "Score:" in sd, sd)
        check("every draft state stays inside the 240-char comfort limit",
              all(len(x) <= 240 for x in (si, sb, sd)),
              f"{len(si)}/{len(sb)}/{len(sd)}")
        # 7d — the guard itself: a real send must be REFUSED while it is armed.
        # (This is the regression test for the 2026-09-26 incident, where a
        # harness run put a stubbed-numbers email in the user's mailbox.)
        set_today("2026-09-27")
        STATE.unlink(missing_ok=True)
        globals()["send"] = real_send          # temporarily un-patch the harness
        n_guard = len(sent)
        refused = real_send("Subject: guard probe\n\nthis must never be sent",
                            "guard-probe.md", False)
        check("real send is refused while the guard is armed", refused is False)
        check("no message recorded by the refused send", len(sent) == n_guard)
        globals()["send"] = fake_send
        # 7e — total return: dividends reinvested, baseline-day ex-date excluded
        check("dividend parse drops the baseline-day ex-date and future dates",
              parse_dividend_rows([
                  {"exOrEffDate": "09/21/2026", "amount": "$0.75143"},  # == baseline: skip
                  {"exOrEffDate": "06/22/2026", "amount": "$0.81349"},  # before: skip
                  {"exOrEffDate": "12/22/2026", "amount": "$0.80"},
                  {"exOrEffDate": "03/23/2027", "amount": "$1.00"},     # future: skip
                  {"exOrEffDate": "bad", "amount": "$1.00"},
                  {"exOrEffDate": "11/19/2026", "amount": "N/A"},
              ], upto="2026-12-31") == [("2026-12-22", 0.80)])
        # compounding maths, hand-checked: identity is (1+TR)/(1+PX) = 1 + d/P1,
        # so the absolute pp gap is exactly d/P0 (not d/P1 — a first-draft test
        # asserted d/P1 and failed, correctly).
        ser = {"2026-09-21": 741.47, "2026-12-22": 700.0}
        f = tr_factor("QQQ", [("2026-12-22", 3.0918)], ser, [])
        exp_tr = (700.0 * (1 + 3.0918 / 700.0) / 741.47 - 1) * 100
        exp_px = (700.0 / 741.47 - 1) * 100
        check("tr_factor compounds at the ex-date close",
              abs(f - (1 + 3.0918 / 700.0)) < 1e-9, f)
        check("total-return gap equals dividend / baseline price",
              abs((exp_tr - exp_px) - (3.0918 / 741.47) * 100) < 0.005,
              f"gap={exp_tr - exp_px:.4f} expected={(3.0918 / 741.47) * 100:.4f}")
        check("total-return gap is positive (equity side credited)",
              exp_tr > exp_px, f"{exp_tr:.4f} vs {exp_px:.4f}")
        # and through the real pipeline: TR must be used as the primary figure
        set_today("2026-12-23")
        STATE.unlink(missing_ok=True)
        stub(spot_v=84000.0, highs={}, divs={"QQQ": [("2026-12-22", 3.0918)]},
             days=[TWEET_DATE, "2026-12-22"])
        snap_tr = build_snapshot(today())
        qqq_tr = snap_tr["equities"]["QQQ"]
        check("pipeline: QQQ total >= price with a dividend present",
              qqq_tr[2] > qqq_tr[3], f"tr={qqq_tr[2]:.4f} px={qqq_tr[3]:.4f}")
        check("pipeline: BTC unaffected by the equity dividend",
              abs(snap_tr["btc_ret"] - (84000.0 / BASE_BTC - 1) * 100) < 0.01)
        check("pipeline: dividend actually moved the number",
              abs(qqq_tr[2] - qqq_tr[3]) > 0.001,
              f"tr={qqq_tr[2]:.4f} px={qqq_tr[3]:.4f}")
        stub(spot_v=84000.0, highs={}, divs={}, days=[TWEET_DATE, "2026-12-22"])
        r_nodiv = build_snapshot(today())["equities"]["QQQ"]
        check("pipeline: with no dividends total return equals price",
              abs(r_nodiv[2] - r_nodiv[3]) < 1e-9,
              f"tr={r_nodiv[2]:.6f} px={r_nodiv[3]:.6f}")
        md_tr = render_monthly(snap_tr, milestone_positions(snap_tr, today()), today(),
                               {}, infl=None, supply=None)
        check("report states the total-return convention",
              "total return" in md_tr and "price-only" in md_tr)
    finally:
        set_today(t_saved)
        globals()["send"] = real_send
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\nselftest: {'ALL PASS' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
    print(f"artifacts: {tmp}")
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Jason tweet-claim monitor")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--monthly", action="store_true")
    g.add_argument("--daily", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    dry = bool(os.environ.get("DRY_RUN"))
    force = bool(os.environ.get("FORCE"))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if a.monthly:
        return run_monthly(dry, force)
    if a.daily:
        return run_daily(dry, force)
    if a.selftest:
        return selftest()
    return run_status()


if __name__ == "__main__":
    sys.exit(main())
