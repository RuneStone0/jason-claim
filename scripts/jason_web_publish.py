#!/usr/bin/env python3
"""Build + publish the Jason-claim tracker webpage data to GitHub.

Imports the monitor, fetches a live snapshot, emits `data.json` (self-contained:
scoreboard, bets, record-high / 57k / CPI, and a since-tweet daily chart) into the
repo checkout, then commits and pushes. Called daily by cron via
jason_web_publish.sh. The static `index.html` renders everything client-side from
data.json, so "updates daily" == a fresh data.json pushed each day.

Env:
  DRY_RUN=1        build data.json only (no commit / push)
  GH_TOKEN         overrides /opt/data/.env (never printed)
"""
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jason_claim_monitor as jm   # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF_URL = "https://x.com/AlexesNakamoto/status/2115053956355231814"  # "11 years"
TOTAL_DAYS = 1095


def _gh_token() -> str:
    tok = os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        for line in open("/opt/data/.env", encoding="utf-8"):
            line = line.strip()
            if line.startswith("GH_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _behind(v, btc_ret):
    return round(v - btc_ret, 2) if (v is not None and btc_ret is not None) else None


def _standing(qqq_ret, ai_ret, btc_ret):
    if qqq_ret is None or ai_ret is None or btc_ret is None:
        return "no close data yet"
    gq, ga = qqq_ret - btc_ret, ai_ret - btc_ret
    if gq > 0 and ga > 0:
        return "both ahead of BTC"
    if gq <= 0 and ga <= 0:
        return "both behind BTC"
    return "split"


def _chart(snap, btc_series, today_d):
    qqq_series = snap["series"].get(jm.QQQ, {})
    per_sym = {}
    for s in jm.AI_BASKET:
        ser = snap["series"].get(s, {})
        for d, px in ser.items():
            if d < jm.TWEET_DATE:
                continue
            per_sym.setdefault(d, {})[s] = px / jm.BASE[s]
    dates = {d for d in btc_series if d >= jm.TWEET_DATE}
    dates |= set(qqq_series)
    dates |= set(per_sym)
    dates = sorted(dates)
    btc, qqq, ai = [], [], []
    for d in dates:
        b = btc_series.get(d)
        btc.append(None if b is None else round((b / jm.BASE_BTC - 1.0) * 100.0, 3))
        q = qqq_series.get(d)
        qqq.append(None if q is None else round((q / jm.BASE[jm.QQQ] - 1.0) * 100.0, 3))
        parts = per_sym.get(d)
        if parts and len(parts) == len(jm.AI_BASKET):
            acc = 1.0
            for f in parts.values():
                acc *= f
            ai.append(round((acc ** (1.0 / len(jm.AI_BASKET)) - 1.0) * 100.0, 3))
        else:
            ai.append(None)
    return {"dates": dates, "btc": btc, "qqq": qqq, "ai": ai}


def build_payload(snap, legs, bets_, today_d, infl, btc_series) -> dict:
    day_of = jm.days_between(dt.date.fromisoformat(jm.TWEET_DATE), today_d)
    btc_ret = snap["btc_ret"]
    btc_spot = snap["btc_spot"]

    qqq_t = snap["equities"].get(jm.QQQ)
    qqq_ret = qqq_t[2] if qqq_t else None
    qqq_price = qqq_t[1] if qqq_t else None
    ai_ret = snap["ai_ret"]
    mags_t = snap["equities"].get(jm.MAGS)
    mags_ret = mags_t[2] if mags_t else None
    mags_price = mags_t[1] if mags_t else None

    ai_parts = {}
    for s in jm.AI_BASKET:
        r = snap["equities"].get(s)
        if r:
            ai_parts[s] = round(r[2], 3)

    # record high (claim B)
    below_pct = round(abs(btc_spot / jm.BTC_ATH - 1.0) * 100.0, 2) if btc_spot else None
    peak = snap.get("btc_peak_since") or {}
    days_b = jm.days_between(today_d, dt.date.fromisoformat(jm.WINDOW_END_B))
    # test 57k (claim C)
    above_pct = round((btc_spot / jm.CLAIM_C_57K - 1.0) * 100.0, 1) if btc_spot else None
    days_c = jm.days_between(today_d, dt.date.fromisoformat(jm.WINDOW_END_C))

    cpi = None
    if infl and not infl.get("error"):
        cpi = {"yoy_pct": round(infl.get("yoy_pct", 0), 2),
               "cum_pct": round(infl.get("cum_pct", 0), 2),
               "source": infl.get("source"),
               "latest_month": infl.get("latest_month")}

    notes = list(snap.get("btc_problems") or [])
    qqq_series = snap["series"].get(jm.QQQ, {})
    if qqq_series and len(qqq_series) == 1 and today_d.isoformat() in qqq_series:
        notes.append("QQQ price is a real-time quote (Nasdaq historical feed was "
                     "down on this run).")

    return {
        "meta": {
            "title": "Jason's tweet claim — tracked in the open",
            "claim_posted": jm.TWEET_DATE,
            "tweet_url": jm.TWEET_URL,
            "tweet2_url": jm.TWEET2_URL,
            "ref_url": REF_URL,
            "as_of": today_d.isoformat(),
            "day_of": day_of,
            "total_days": TOTAL_DAYS,
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "baselines": {
                "QQQ": jm.BASE[jm.QQQ], "BTC": jm.BASE_BTC,
                "record_high": jm.BTC_ATH, "ath_date": jm.BTC_ATH_DATE,
                "btc_ath_alt": jm.BTC_ATH_ALT,
                "note": "closes frozen at the 2026-09-21 close.",
            },
        },
        "standing": _standing(qqq_ret, ai_ret, btc_ret),
        "scoreboard": {
            "btc": {"price": btc_spot, "ret_pct": round(btc_ret, 2) if btc_ret is not None else None},
            "qqq": {"price": qqq_price, "ret_pct": round(qqq_ret, 2) if qqq_ret is not None else None,
                    "behind_btc_pct": _behind(qqq_ret, btc_ret)},
            "ai": {"ret_pct": round(ai_ret, 2) if ai_ret is not None else None,
                   "behind_btc_pct": _behind(ai_ret, btc_ret),
                   "basket": list(jm.AI_BASKET), "parts": ai_parts},
            "mags": {"price": mags_price, "ret_pct": round(mags_ret, 2) if mags_ret is not None else None,
                     "behind_btc_pct": _behind(mags_ret, btc_ret)},
        },
        "bets": {
            "counts": bets_.get("counts", {}),
            "headline": bets_.get("headline", ""),
            "rows": bets_.get("rows", []),
        },
        "record_high": {
            "ath": jm.BTC_ATH, "btc": btc_spot, "below_pct": below_pct,
            "below_usd": round(jm.BTC_ATH - btc_spot, 0) if btc_spot else None,
            "peak_since": {"date": peak.get("date"), "high": peak.get("high")},
            "days_left": days_b,
        },
        "test_57k": {
            "threshold": jm.CLAIM_C_57K, "btc": btc_spot, "above_pct": above_pct,
            "days_left": days_c,
        },
        "cpi": cpi,
        "chart": _chart(snap, btc_series, today_d),
        "notes": notes,
        "disclaimer": ("Tracked in the open. Equity legs are "
                       "total return (dividends reinvested); verdicts stay nominal."),
    }


def _run(cmd, env, cwd):
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=120)


def main() -> int:
    checkout = sys.argv[1] if len(sys.argv) > 1 else REPO
    dry = bool(os.environ.get("DRY_RUN"))
    t = jm.today()
    try:
        snap = jm.build_snapshot(t)                    # total-return snapshot
        legs = jm.milestone_positions(snap, t)
        b = jm.bets(snap, legs, t)
        btc_series = jm.btc_daily(max(40, jm.days_between(dt.date.fromisoformat(jm.TWEET_DATE), t) + 3))
    except Exception as e:                              # noqa: BLE001
        print(f"PUBLISH FAILED: fetch error {repr(e)[:240]}", file=sys.stderr)
        return 1
    infl = None
    try:
        infl = jm.inflation_block()
    except Exception as e:                              # noqa: BLE001
        print(f"publish: CPI unavailable ({repr(e)[:120]}), omitting", file=sys.stderr)

    payload = build_payload(snap, legs, b, t, infl, btc_series)
    os.makedirs(checkout, exist_ok=True)
    out = os.path.join(checkout, "data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"wrote {out} (as_of {payload['meta']['as_of']}, "
          f"day {payload['meta']['day_of']}, QQQ {payload['scoreboard']['qqq']['ret_pct']}%, "
          f"AI {payload['scoreboard']['ai']['ret_pct']}%, "
          f"BTC {payload['scoreboard']['btc']['ret_pct']}%)")

    if dry:
        print("DRY RUN — data.json written, no commit/push")
        return 0

    tok = _gh_token()
    if not tok:
        print("PUBLISH FAILED: no GH_TOKEN", file=sys.stderr)
        return 1
    env = {**os.environ, "GH_TOKEN": tok,
           "GIT_AUTHOR_NAME": "Alfred", "GIT_AUTHOR_EMAIL": "alfred@rtk-cv.dk",
           "GIT_COMMITTER_NAME": "Alfred", "GIT_COMMITTER_EMAIL": "alfred@rtk-cv.dk"}
    r = _run(["git", "add", "-A"], env, checkout)
    r = _run(["git", "diff", "--cached", "--quiet"], env, checkout)
    if r.returncode == 0:
        print("no data change — nothing to push")
        return 0
    r = _run(["git", "commit", "-m", f"data: standing {payload['meta']['as_of']} (day {payload['meta']['day_of']})"],
              env, checkout)
    if r.returncode != 0:
        print(f"commit failed: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    r = _run(["git", "push", "origin", "HEAD:main"], env, checkout)
    if r.returncode != 0:
        print(f"push failed: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
        return 1
    print("pushed to origin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
