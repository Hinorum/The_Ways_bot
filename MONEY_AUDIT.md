# Money / Payments / TON Subsystem — Security & Ledger Audit

Repo: `E:\User\Documents\The_Ways_bot`
Scope: stake deposits, wallet matching, revote/switch (Stars + TON memo), treasury reconciliation, weekly/monthly leaderboard payouts, float↔nano precision.
Method: static analysis only — **no code changes made.**

---

## Summary

The subsystem is unusually defensive and well-tested. The ledger is mostly self-consistent:
- `treasury_expected_state` (`app/ops.py:393`) deliberately counts each incoming transfer once (Income rows) and nets it against sent/unpaid Payout rows; the gas gap of outbound transfers is covered by `_BALANCE_TOLERANCE_NANO + sent_count * 2 * payout_fee` (`app/ops.py:476`), matching the "deduct fee from the pool, then burn fee again on broadcast" model in `finalize_day_payouts` (`app/stakes.py:269-271`).
- No float arithmetic reaches the chain: every amount passes through `to_nano`/integer nanotons at the boundary (`app/stakes.py`, `app/ton_utils.py:81-86`); nanoton dust is deterministically assigned to the largest share / smallest player id.

The genuine findings below are concentrated in: wallet-collision UX, cross-circuit (`network`) gaps, the asymmetric revote-memo ceiling, and migration-failure availability. None is a free-money drain; the strongest is MED.

---

## Findings (severity-ranked)

### MED-1 — Revote *memo* path is not bounded by the revote ceiling, so a large `rv:` transfer never becomes a stake
`app/ton_watch.py:705-718` (`_process_revote`) checks only the **lower** bound:
```python
if transfer.value_nanotons < to_nano(settings.revote_ton):
    return "revote_too_small"
```
There is **no `value < to_nano(stake_min_ton)` check** here. Contrast with the amount-based auto-grant fallback at `app/ton_watch.py:525`, which *does* restrict to `to_nano(revote_ton) <= value < to_nano(stake_min_ton)`.

- **Trigger:** open day, player already has a vote (`Vote` row), sends a transfer **≥ `stake_min_ton` (0.5 Gram)** with memo `rv:<round_id>` (e.g. `rv:712`) intending to both revote *and* stake.
- **Behaviour:** `process_transfer` (`app/ton_watch.py:494-496`) sees a parseable `rv:` memo and returns via `_process_revote` → `_grant_revote`: one `RevoteGrant` created, one `Income(kind="ton", amount=full value)` recorded (`app/ton_watch.py:690-699`), **and no `Stake` row**. The full amount becomes "revote income" and the player gets only a path-switch right — their intended stake never participates in the day's fund.
- **Why it matters:** the `on_payton` prompt (`app/handlers.py:1466-1472`) tells players "ровно 0.5 Gram подойдёт... это уже минимальная ставка" and the ceiling is `revote_gram_ceiling()=0.49` (`app/handlers.py:1237-1244`) — but a player who puts the `rv:` memo on a staking-sized amount bypasses that intent. The two payment paths are asymmetric on the same boundary.
- **Real-bug vs by-design:** real inconsistency (the explicit-memo path and the amount-path disagree about where the revote band ends). Not a treasury loss (the money is still income), but it can convert a legitimate stake into a non-stake and silently forfeit stake participation.
- **Coverage:** `test_revote.py` (`test_paid_revote_via_card_press`, `test_successful_payment_creates_single_grant`, `test_parse_revote_memo_tolerates_wallet_noise`) and `test_ton.py` (`test_auto_grant_by_amount_when_memo_missing`, `test_auto_grant_returns_refund_if_no_vote`) exercise small in-band transfers. **No test covers a ≥ `stake_min_ton` transfer carrying a `rv:` memo.**

### MED-2 — Wallet duplicate-bind `IntegrityError` is uncaught → dead dialog, no feedback, no money loss
`app/handlers.py:854-898` (`_bind_wallet`) writes `player.wallet_address` and `session.commit()` with **no `IntegrityError` handler** for `Player.wallet_address`'s `unique=True` (`app/models.py:61`). The dialog call site at `app/handlers.py:2652` is not wrapped in try/except either.
- **Trigger:** player A binds address raw `0:abcd...`; player B later binds the **same raw hex** (any of its friendly forms normalize to it via `normalize_address`, `app/ton_utils.py:30-42`).
- **Behaviour:** commit raises `IntegrityError`; aiogram global error handler fires; B gets no meaningful message, and the wallet dialog stays open (retried on next message). A's binding is untouched. No funds move.
- **Real-bug vs by-design:** by-design the constraint exists to prevent two wallets being credited for one source; the *handling* is a UX defect.
- **Coverage:** `test_wallet_flow.py` (`test_wallet_without_args_opens_dialog`, `test_next_message_binds_address`, `test_bad_address_keeps_dialog_open`), `test_ton.py::test_wallet_rebind_locked_with_active_stake`, `test_privacy.py::test_wallet_bind_*`. **No test binds the same normalized address twice** (the collision path is untested).

### MED-3 — `_migrate_wallet_formats` fails hard if two players already hold the same raw address
The one-time friendly→raw migration (`app/ton_watch.py`, `_migrate_wallet_formats`) rewrites every `Player.wallet_address` to its normalized form in one commit. A pre-migration DB can hold two players with the *same underlying address in different friendly forms* (e.g. A=`UQ…`, B=`EQ…` of the same raw) — legal before the `unique=True`-normalized invariant. After normalization both become identical raw → bulk `commit` raises `IntegrityError` → **watcher cannot start** (migration is part of boot).
- **Trigger:** legacy DB (pre-normalization) with such a friendly-form duplicate.
- **Behaviour:** boot-time migration commit fails → the ingest/watcher loop never starts → all staking/revote/refund handling halts until an operator reconciles the rows.
- **Real-bug vs by-design:** availability hazard on upgrade, not a money loss.
- **Coverage:** `test_wallet_match.py::test_old_friendly_row_would_miss_and_migration_fixes_it` covers the happy-path migration; **no test exercises the duplicate-normalized-address failure.**

### MED-4 — `confirm_aged_pending` has no `network` filter (isolated-circuits gap)
`app/ton_watch.py:635-667`: `select(Stake).where(Stake.status == "pending")` — no `Stake.network == current_network()`. All other voters/finalizers honor the network split (`app/stakes.py:185`, `:369`, `:391`; `app/tally.py:254`, `:318`).
- **Trigger:** one shared DB with both testnet and mainnet circuits; a "pending" testnet stake ages past `stake_confirm_seconds` while a **mainnet** confirmation tick runs and that testnet round is OPEN.
- **Behaviour:** the testnet stake is marked `confirmed` (and the player DM'd) by the mainnet circuit. Because `finalize_day_payouts` filters by `Stake.network == current_network()` (`app/stakes.py:185`), the money is not misdirected — the stake is still finalized by whichever circuit owns its network. Impact is limited to cross-circuit confirmation timing and messaging.
- **Real-bug vs by-design:** violates the documented "isolated circuits" intent (`app/stakes.py:26-28`) though without monetary corruption.
- **Coverage:** `test_wallet_match.py::test_fresh_stake_waits_then_aged_pass_confirms_and_notifies` runs on a single network; `test_ton.py::test_networks_are_isolated` proves finalization isolation but **not** the confirmation pass.

### LOW-1 — `settle_month_if_due`: `payable_ids` gate ignores `total == 0`
`app/leaderboard.py:128` sets `total` from empty pots → `0`; the `if not payable_ids` guard (`:147`) does not check `total > 0`. With winners that have wallets and no pot, `split_equal(0, ids)` returns `{}` (`app/stakes.py:96`), so no payouts are created, yet the marker advances and empty pots are deleted (`app/leaderboard.py:169-176`).
- **Trigger:** no `LeaderboardPot` rows at all + top voters exist with wallets.
- **Behaviour:** marker moves to `prev_key` on an empty settlement — harmless (no zero-value payouts due to `split_equal` returning `{}`).
- **Real-bug vs by-design:** benign no-op; only a semantic wart.
- **Coverage:** month path covered by `test_ops.py::test_monthly_pot_*`; the `total==0` branch is not explicitly tested.

### LOW-2 — Refund dedupe compares full hash to a `[:80]`-stored hash
`app/ton_watch.py:384-385` dedupes `Payout.tx_hash == transfer.tx_hash` (full string) but stores `tx_hash=transfer.tx_hash[:80]` (`:396`). Real TonAPI/Toncenter hashes are 64 hex chars (`_norm_tx_hash`, `app/ton_watch.py:218-243`), so `[:80]` never truncates and the comparison is symmetric in practice.
- **Trigger:** only if a >80-char synthetic hash were injected (tests use short sentinels like `"fresh"`, which is what makes this asymmetry live in tests).
- **Behaviour:** none for real transactions; the slice is the latent inconsistency.
- **Coverage:** `test_incoming_ledger.py::test_ledger_is_idempotent_by_tx_hash`, `test_payout_admin.py::test_stash_refund_skips_ancient_transfers` (short sentinel hashes pass through both sides identically).

### LOW-3 — `_stash_refund` pays full value (no gas), unlike day-close refunds
`app/ton_watch.py:389-399`: auto/early refund `Payout` uses `amount_nanotons = transfer.value_nanotons` with **no** gas deduction, whereas day-close refunds use `refund_net_amount` (= amount − `payout_fee_gram`) (`app/stakes.py:62-71`, `:338-340`).
- **Behaviour:** immediate auto-refunds return the gross amount; treasury reconciliation still nets out (the matching `Income` + `Payout` cancel in `treasury_expected_state`). The gas burned on these refunds falls within the `sent_count * 2 * fee` tolerance.
- **Real-bug vs by-design:** intentional (an immediate refund for a paused game / unknown sender returns the full user deposit rather than nickel-and-diming gas).
- **Coverage:** `test_treasury_adjust.py::test_manual_refund_creates_net_payout_and_is_idempotent` (manual) and `test_ton.py::test_unknown_sender_transfer_is_auto_refunded` (auto full-value).

---

## Areas verified solid (no action)

- **Float precision:** `to_nano`/`from_nano` (`app/ton_utils.py:81-86`); all pot splits use integer nanotons with deterministic dust (`split_pot` `app/stakes.py:74-89`, `split_equal` `:92-102`). Property-conservation tests exist: `test_ton.py::test_split_pot_conserves_money_property`, `test_split_equal_conserves_money_property`, `test_week_prize_amounts_dust_and_rollover`.
- **Fund split & fee netting:** 96/1/2/0.5/0.5 and gas pre-deduction, sub-`min_payout_gram` dust to weekly pot, no-winner full refund minus gas — `test_fee_netting.py` (`test_fee_is_deducted_proportionally`, `test_dust_shares_roll_to_weekly_pot`, `test_gas_eaten_pool_goes_to_week_and_is_not_refund`, `test_refunds_deduct_gas`).
- **Week/month settlement & rollover:** ISO keying, marker idempotency, tier splitting, empty-step rollover, "wait until last day finalized" — `test_weekly.py` (6 tests), `test_ops.py::test_monthly_pot_*`, `test_previous_month_key`.
- **Incoming ledger dedupe & unknown-sender logging:** `test_incoming_ledger.py` (3 tests).
- **Payout lifecycle & memo dedupe:** pending→sending(marked before broadcast)→sent/failed, retry dedupe, `resolve_dead_payout` spam/retry, dismissed not a debt — `test_ops.py::test_failed_payout_is_retried_until_limit`, `test_stuck_sending_is_revived_and_sent`, `test_admin_alerted_once_per_payout`, `test_payout_dedupe.py::test_dispatch_marks_sent_when_memo_already_broadcast`, `test_payout_admin.py` (3 tests).
- **Wallet matching & normalization:** raw-hex canonical form, friendly-forms→raw equivalence, rebind lock w/ active stake — `test_wallet_match.py` (5 tests), `test_ton.py::test_wallet_rebind_locked_with_active_stake`, `test_ton_pay_wallets.py` (v4/v5 detection).
- **Treasury reconciliation:** no double-count of confirmed stakes (each transfer one Income row), network-filtered sums, tolerance formula — `test_treasury_adjust.py::test_single_stake_not_double_counted`, `test_treasury_expected_state_filters_by_network`, `test_chain_guards.py::test_income_revotes_counted_in_expected_float`, `test_anomaly_flags_deficit_and_drift_and_stays_quiet_when_funded`.
- **Streaming/source fallback & tx-hash normalization:** TonAPI↔Toncenter fallback, cursor advancement, jetton filtering — `test_watch_sources.py` (8 tests), `test_ops.py::test_watch_once_advances_cursor_and_dedupes`, `test_watch_cursor_stops_on_failure`, `test_jetton_transfer_is_not_a_stake_and_not_refunded`.

---

## Suggested follow-ups (analysis-only, not applied)

1. **MED-1:** add an upper-bound check in `_process_revote` mirroring the amount-path band, or route a `rv:`-memo transfer that is ≥ `stake_min_ton` back into the stake path. Add a test: large `rv:` memo → expect stake + (optional) revote rather than all-revote.
2. **MED-2:** catch `IntegrityError` in `_bind_wallet` and reply "этот кошелёк уже привязан к другому игроку" (and close the dialog). Add a duplicate-bound test.
3. **MED-3:** make the wallet-format migration tolerant of duplicate normalized raws (report & quarantine rows instead of aborting boot). Add a migration-duplicate test.
4. **MED-4:** add `Stake.network == current_network()` to `confirm_aged_pending` and a two-circuit test.
