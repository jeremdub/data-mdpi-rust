# dot15d4 TSCH: experiment data

Raw data and analysis scripts for *Towards Memory-Safe IIoT: An IEEE 802.15.4
TSCH MAC Layer with Hardware-Determined Timing in Rust*. Every experiment has
its own folder, holding the script that ran it and the raw logs, analyzer
exports and captures of each of its runs. Each `check_*.py` recomputes, from
those raw files, the values the paper reports.

## Layout

    check_*.py             recompute the tables of one experiment
    count_unsafe.py        recompute the unsafe counts of Table 12
    v1-*, v2-*, e1-*..e5-* one folder per experiment of Table 4
    dot15d4/               the Rust MAC source used for the evaluation
    contiki-tsch-trace/    the Contiki-NG baseline and peer, with its own README

## Recomputing the paper's tables

Python 3.8 or later. Only `check_e5.py` needs anything outside the standard
library (NumPy). Run every script from this folder. Without arguments, each
reads the ten runs of its experiment and prints the values of each run, then
the pooled values and the 95% confidence intervals the paper reports.

| | Table | Folder | Runs | Recompute |
|---|---|---|---|---|
| V1, V2a, V2b | 5, S6, S7 | `v1-validation_procedures`, `v2-validation_shared_dedicated_cells` | 10 x 1 h, 2 x 10 x 30 min | `python3 check_v.py` |
| E1 | 6, S2 | `e1-determinism` | 10 x 3 h per board | `python3 check_e1.py` |
| E2 | 7, S8 | `e2-sync_error` | 10 x 6 h | `python3 check_e2.py` |
| E2h | 7, S8 | `e2h-sync_error-hopping` | 10 x 6 h | `python3 check_e2.py e2h-sync_error-hopping` |
| E3a | 8, S9 | `e3a-dot15d4_47slots` | 10 x 4 h | `python3 check_e2.py e3a-dot15d4_47slots` |
| E3b, E3c | 8, S9 | `e3b-e3c-sync_error_contiki` | 2 x 10 x 4 h | `python3 check_e3.py` |
| E4 | 9, S10 | `e4-interoperability/data` | 3 x 10 x 1 h | `python3 check_e4.py` |
| E5 | Section 5.5 | `e5-energy/ppk2-2026*.ppk2` | 10 x 10 min | `python3 check_e5.py` |
| E5 | 10 | `e5-energy/ppk2-100khz-5min.ppk2` | 1 x 5 min at 100 kHz | `python3 check_e5.py e5-energy/ppk2-100khz-5min.ppk2` |
| E5, 200 us guard time | Section 5.5 | `e5-energy/ppk2-guard200us-100khz-5min.ppk2` | 1 x 5 min at 100 kHz | `python3 check_e5.py --guard 200 e5-energy/ppk2-guard200us-100khz-5min.ppk2` |
| Unsafe surface | 12 | `dot15d4` | | `python3 count_unsafe.py` |

E3a is the E2 star on a 47-slot slotframe, so `check_e2.py` reads both. E4 has
three variants: A, and B before and after the Contiki-NG RMARKER fix.

Two longer single runs back the supplementary material. Both take the same
scripts:

| | Folder | Run | Recompute |
|---|---|---|---|
| E1, Table S2 | `e1-determinism/longer-run` | 1 x 6 h, analyzer only | `python3 check_e1.py e1-determinism/longer-run` |
| E2, Table S8 and Figure S2 | `e2-sync_error/longer-run` | 1 x 24 h | `python3 check_e2.py e2-sync_error/longer-run` |


Table 12 counts commit `d47e523`, the evaluated source and the first commit of
this repository; `dot15d4/` adds the fixes of Sections 5.5 and 5.7. To count
that commit:

    git archive <first commit> dot15d4 | tar -x -C /tmp
    python3 count_unsafe.py /tmp/dot15d4

## Firmware

dot15d4 nodes run `examples/nrf52840`, binary `tsch-node`, built with the
`[profile.release]` of the workspace (`debug-assertions` and `overflow-checks`
on, `panic = "abort"`). Every node has `defmt`, `tsch-log` and
`sync-error-log`, and `tsch` or the `tsch-coordinator` that implies it; the
table gives what each experiment adds. Compile-time constants are overridden
through environment variables, and every log repeats the feature list in its
`Compiling Embassy application` line. `NODE_ID` numbers the boards, and
`PARENT` pins the time source wherever the cell layout is static.

| | Adds | Overrides | Slotframe |
|---|---|---|---|
| V1 | `tsch-coordinator` (root, relay), `tsch-association` (relay, leaf) | `CHANNEL=20 BEACON_PERIOD_S=10 SCHEDULE=minimal\|learn STATS_PERIOD_MS=600000 DOT15D4_MAC_TSCH_KEEP_ALIVE_PERIOD=400 SENDER=1 SEND_PERIOD_MS=30000 SEND_JITTER_MS=0`; root and relay `BEACON_JITTER_MS=500`; leaf `BEACON=0` | 11 |
| V2a, V2b | `tsch-coordinator` (root), `tsch-association` (leaves) | `CHANNEL=20 BEACON_PERIOD_S=10 SLOTFRAME_SIZE=8 SENDER=1 SEND_PERIOD_MS=250 SEND_JITTER_MS=100 SCHEDULE=star` (V2a, dedicated cells) or `SCHEDULE=contention` (V2b, one shared cell); root `NODE_COUNT=3 STATS_PERIOD_MS=60000`; leaves `BEACON=0` | 8 |
| E1 | `tsch-coordinator` (root), `tsch-ramp-trace` | `SCHEDULE=chain STATS_PERIOD_MS=120000`; leaf `BEACON=0 DOT15D4_MAC_TSCH_KEEP_ALIVE_PERIOD=700` | 101 |
| E2 | `tsch-coordinator` (root), `radio-trace` | `SCHEDULE=star STATS_PERIOD_MS=20000 DOT15D4_MAC_TSCH_MAX_LINKS=13`; leaves `BEACON=0` | 101 |
| E3a | as E2 | as E2, plus `SLOTFRAME_SIZE=47` | 47 |
| E4 | `tsch-coordinator` (variant A), `radio-trace` | `DOT15D4_MAC_PAN_ID=0xabcd STATS_PERIOD_MS=120000`; coordinator `SCHEDULE=chain`; leaf `SCHEDULE=learn BEACON=0 SENDER=1 SEND_PERIOD_MS=15000 BEACON_JITTER_MS=2000` | 101 (variant A), 47 learnt from the Contiki-NG beacon (variant B) |
| E5 | none | defaults | 101 |

Only V1 and V2 pin a channel. Everywhere else `CHANNEL` is unset, which is the
single-entry hopping sequence {26} the MAC defaults to, so one sniffer parked
on channel 26 records the whole run.

Contiki-NG nodes (E3b, E3c, and the peer of E4) are built from commit
`7f844118d`, patched with the tracing layer of `contiki-tsch-trace/` and with
the two nRF port fixes submitted upstream as PRs #3277 and #3278. The rtimer
rate is `RTIMER_SECOND=62500`, the 16 us tick the port ships, for E3b, and
`1000000`, one tick per microsecond, for E3c and E4. Its slotframe is 47
timeslots and carries one shared advertising cell.

Unless a row says otherwise: the 10 ms timeslot template, a 2200 us guard time,
a drift estimator over 8 samples, and a 10 s beacon period. Every timing and
synchronization statistic discards the first 600 s of its run.

## Re-running an experiment

`experiment.py` drives the boards through [`ioteapot`](https://github.com/jeremydub/ioteapot),
our bench harness, which is public and needs Python 3.11 or later. The scripts
are here as the record of how each run was produced: the roles, the features,
the overrides and the run length. Board serials and paths at the top of each
file are those of our bench. The command that produced each set of logs is the
first line of its `log.txt`.

`v1-validation_procedures` and `v2-validation_shared_dedicated_cells` hold the
same script, whose `--variant` selects `v1`, `v2-dedicated` (V2a) or
`v2-shared` (V2b). E3b, E3c and E4 take `--coordinator` and `--leaf`, each
`rust` or `contiki`: `e3b-e3c-sync_error_contiki/experiment.py` runs
Contiki-NG in both roles, and `e4-interoperability/experiment.py` swaps one
role for the other and makes the joined node send data frames. Both need
`CONTIKI` in the environment, pointing at the patched tree.

## The Rust source

`dot15d4/` is the MAC source the evaluation runs on, with its own README, and
`dot15d4/fuzz/` the four `cargo-fuzz` targets of Section 5.7.
