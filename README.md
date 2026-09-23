# Strat 1 — Live Forward Test

**Members (5): Pivot S/R, Overnight Extension, Divergence-Fade, COT Positioning Extreme, Gold MM Positioning**
Backtested: CAGR 9.89%, MaxDD exactly -6.00%, Sharpe 1.632.

This repo replaces the old `portfolio-forward-test` repo entirely. That repo
had accumulated 15+ legacy scripts unrelated to what's actually running, two
strategies (ADX+Supertrend, QM+CISD+SBR) that were removed after a
mechanical audit found real lookahead bugs, and a README describing an
even earlier project state. This one contains only what's live, each piece
individually audited, with the reasoning kept alongside the code rather
than lost to chat history.

## Admission rule — read before ever adding a strategy here

A new strategy is added to Strat 1 **only if** re-running the full exhaustive
combinatorial search — across the current members plus the new candidate —
finds a combination that **genuinely improves CAGR at the exact same 6% MaxDD
ceiling**, verified by the same binary-search-to-exact-6%-MaxDD method used
to build this configuration. Not "looks good alone." Not "positive Sharpe."
A real, re-verified improvement to the actual combined number.

If it doesn't pass: it goes in **Capital** instead (a separate repo/config,
not yet built — blocked on sourcing 4 more strategy scripts), which has no
admission bar because it has no drawdown ceiling to protect.

Every strategy not currently listed here (Donchian, Connors RSI, Monday
Effect, RSI(2), TGA-Follow, VIX Shock-Fade) has already been tested against
this bar and did not improve on the current members.

## Audit trail — two checks a rigorous framework prescribes, actually run

**1. Was the admission search itself overfit?** Re-ran the exhaustive
combinatorial search using ONLY 2016-2023 data (training), completely blind
to 2024-2026. The same 4-strategy core (Pivot S/R, Overnight Extension,
Divergence-Fade, COT) emerged as best. Applied to the genuinely held-out
2024-2026 period with no re-tuning: CAGR 13.77% (vs. 8.57% in training),
MaxDD -5.72% (within the 6% budget). This is real out-of-sample evidence,
not a search artifact. Gold MM Positioning's addition was tested the same
way: adding it to the base 4 produced a small, genuine, held-out-period
improvement (13.77% -> 13.87%) - thin, but real and never seen during
selection.

**2. Is this actually diversified, or one bet in disguise (the
"diversification illusion")?** Ran PCA on the 5 strategies' standardized
return streams. Variance is spread almost evenly across all 5 components
(18-22% each) - close to what genuinely independent series would produce.
The first component technically clears a noise band (built by decomposing
500 runs of pure random noise of the same shape) but only marginally
(21.9% vs 21.4%) - a weak, not dominant, shared factor. Contrast: running
the same test on the 11-strategy Capital universe found a clear, confirmed
shared factor (20.4%, well above its own 10.1% noise band) driven almost
entirely by 4 specific strategies (Connors RSI, Donchian, Pivot S/R, Monday
Effect) that are NOT part of Strat 1 - exactly the failure mode this check
is designed to catch, and exactly why those 4 aren't here.

## What's in this repo - built, integrated, and audited individually

- `journal.py` - risk/equity logic, Strat 1 sizing only, 10% total-open-risk
  cap, 5x-starting-capital compounding cap
- `pivot_sr_signals.py`, `nas100_state.json`, `nas100_signal_log.csv` (seeded,
  520 days real NAS100 history), `.github/workflows/pivot_sr_signals.yml`
  - rewritten from the original standalone version to use the shared
  `journal.py` risk/equity system (the original never did - it sized and
  logged independently, invisible to the 10% total-risk cap). Signal logic
  itself (pivot/resistance/vol-scale) unchanged. Entry and exit both tested
  end-to-end through the shared journal.
- `cot_signals.py`, `cot_full_state.json`, `.github/workflows/cot_signals.yml`
  - field-name bug fixed 2026-09-22: the live TFF API's real field names are
  `lev_money_positions_long`/`_short` (no `_all` suffix), confirmed directly
  against the live API and against a real production run on MT5.
- `divergence_signals.py`, `divergence_full_state.json`,
  `.github/workflows/divergence_signals.yml`
- `overnight_extension_signals.py`, `overnight_extension_state.json`,
  `.github/workflows/overnight_extension_signals.yml` - audited for the
  specific lookahead pattern that broke ADX+Supertrend/QM+CISD+SBR:
  confirmed the entry check never fires on the same bar that sets its own
  trigger level. Clean.
- `gold_mm_signals.py`, `gold_mm_log.csv` (seeded, 559 weeks real CFTC
  Disaggregated history), `gold_mm_state.json`,
  `.github/workflows/gold_mm_signals.yml` - tested end-to-end. Confirmed
  working in production against the live CFTC API via the MT5 local setup.
- `daily_pnl_summary.py`, `.github/workflows/daily_pnl_summary.yml`
- `equity.json`, `journal.csv` - fresh, GBP 1,000,000 starting equity

## What's NOT in this repo - copy unchanged from the old repo if needed

- `execution.py`, `oanda_client.py`, `instrument_map.py` - only needed for
  live OANDA execution; never rebuilt in this conversation

## Also running, separately - MT5 local paper trading

A parallel, independent setup runs the same 5 strategies via MT5 on a local
Windows machine (Task Scheduler instead of GitHub Actions, `execution_mt5.py`
instead of `execution.py`/OANDA). Confirmed working end-to-end, including a
real production catch of the COT field-name bug above. See that setup's own
README for details - it is entirely separate infrastructure and does not
share state, credentials, or a risk budget with this repo.

## Setup - fresh repo

1. Create a new, empty GitHub repo (e.g. `strat1-live`).
2. Add every file listed above, preserving the `.github/workflows/`
   subfolder structure exactly.
3. Add the 3 execution files from the old repo if using live OANDA
   execution; skip for signal-only.
4. Add secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (and
   `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENVIRONMENT` if using
   execution).
5. Settings -> Actions -> General -> Workflow permissions -> Read and write.
6. Run every workflow manually once (Actions tab -> workflow -> Run
   workflow), confirm all green, confirm Telegram messages arrive, confirm
   state files show a fresh commit timestamp.
7. Only then trust the schedule.
