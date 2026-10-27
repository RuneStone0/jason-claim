# Jason's claim — tracked in the open

A live, self-updating tracker for a public Bitcoin call. On
[2026-09-21](https://x.com/Jason/status/2102051827671871601) `@Jason` claimed:

> *"90% chance that $QQQ and top AI companies will beat $BTC over the next 12, 24
> and 36 months"* — and *"Bitcoin won't break its record high in next 12 months."*

On [2026-10-27](https://x.com/jason/status/2114956980674605075) he doubled down:
*"We will test the 57k low again in the next 12 months"* and *"Bitcoin will never
break its all-time high again."*

That is **six checkable bets**. This page scores all six against frozen baselines
and updates daily.

## How it works

- `index.html` — a single, dependency-free page (dark, mobile-first, responsive).
  It reads `data.json` and renders everything client-side.
- `data.json` — the current snapshot: the scoreboard (BTC / QQQ / top-AI /
  MAGS), the six bets, Bitcoin vs his record-high and $57k levels, a real-terms
  (CPI) note, and a since-the-tweet daily chart. Regenerated daily by an
  automated monitor and pushed here.
- Baselines are frozen at the **2026-09-21 close** (the day of the tweet) so the
  comparison can never drift with a data-source revision.

## Conventions (so the score is honest)

- **Equity legs are total return** — every dividend after the baseline is
  reinvested at its ex-date close (Nasdaq's dividend record). BTC pays no
  distributions, so it is unaffected.
- "Top AI companies" is read as an **equal-weight basket of 9**: NVDA, MSFT,
  GOOGL, AMZN, META, AVGO, AMD, TSM, SPCX (SpaceX+xAI, post-merger). SPCX is
  itself a Nasdaq-100 member, so QQQ already holds it — those two lines overlap
  by design.
- Verdicts stay **nominal** (the bet is written in market prices); the CPI note
  is context, never a scoreboard change.
- Each 12/24/36-month bet needs **both** QQQ *and* the AI basket above BTC at
  that mark. The record-high and "never break ATH" bets can only be lost early
  (by a break); "test $57k" can only be won early (by a print at/below $57,000).

## Not financial advice

This is a public-record project, not a prediction of its own and not investment
advice.

*Data sources: Nasdaq (historical + real-time quotes), CoinGecko (BTC), BLS/FRED
(CPI).*
