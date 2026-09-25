# dot15d4 TSCH: experiment data

Raw data and analysis scripts for *Towards Memory-Safe IIoT: An IEEE 802.15.4
TSCH MAC Layer with Hardware-Determined Timing in Rust*. Every experiment has
its own folder, holding the script that ran it and the raw logs, analyzer
exports and captures of each of its runs. Each `check_*.py` recomputes, from
those raw files, the values the paper reports.

## Layout

    check_*.py             recompute the tables of one experiment
    v1-*, v2-*, e1-*..e5-* one folder per experiment of Table 3
    dot15d4/               the Rust MAC source used for the evaluation
    contiki-tsch-trace/    the Contiki-NG baseline and peer, with its own README

## Recomputing the paper's tables

Python 3.8 or later. Only `check_e5.py` needs anything outside the standard
library (NumPy, for the eight million samples of a power capture). Run every
script from this folder. The repeated experiments print the values of each run
first and the pooled values the paper reports last.

| | Table | Folder | Runs | Recompute |
|---|---|---|---|---|
| V1, V2a, V2b | 4 | `v1-validation_procedures`, `v2-validation_shared_dedicated_cells` | 3 x 1 h, 2 x 3 x 30 min | `python3 check_v.py` |
| E1 | 5 | `e1-determinism` | 3 x 3 h per role | `python3 check_e1.py e1-determinism/run1 e1-determinism/run2 e1-determinism/run3` |
| E2 | 6 | `e2-sync_error` | 3 x 6 h | `python3 check_e2.py e2-sync_error/run1 e2-sync_error/run2 e2-sync_error/run3` |
| E3a | 7 | `e3a-dot15d4_47slots` | 3 x 4 h | `python3 check_e2.py e3a-dot15d4_47slots/run1 e3a-dot15d4_47slots/run2 e3a-dot15d4_47slots/run3` |
| E3b | 7 | `e3b-e3c-sync_error_contiki/e3b-run*` | 3 x 4 h | `python3 check_e3.py e3b-e3c-sync_error_contiki/e3b-run1 e3b-e3c-sync_error_contiki/e3b-run2 e3b-e3c-sync_error_contiki/e3b-run3` |
| E3c | 7 | `e3b-e3c-sync_error_contiki/e3c-run*` | 3 x 4 h | `python3 check_e3.py e3b-e3c-sync_error_contiki/e3c-run1 e3b-e3c-sync_error_contiki/e3c-run2 e3b-e3c-sync_error_contiki/e3c-run3` |
| E4 | 8 | `e4-interoperability` | 3 x 2 h | `python3 check_e4.py e4-interoperability/data/*-log.txt` |
| E5 | 9 | `e5-energy` | 1 x 82 s, 2 x 10 s | `python3 check_e5.py e5-energy/e5-82s.ppk2` |

E3a is the E2 star on a 47-slot slotframe, so `check_e2.py` reads both. E4 ran
three times: variant A once, and variant B before and after the Contiki-NG
RMARKER fix.

Two longer single runs back the supplementary material. Both take the same
scripts:

| | Folder | Run | Recompute |
|---|---|---|---|
| E1, Table S2 | `e1-determinism/longer-run` | 1 x 6 h, analyzer only | `python3 check_e1.py e1-determinism/longer-run` |
| E2, Table S8 and Figure S2 | `e2-sync_error/longer-run` | 1 x 24 h | `python3 check_e2.py e2-sync_error/longer-run` |

`check_e5.py` takes one capture at a time. `e5-82s.ppk2` is the capture of
Section 5.5 and Table 9, `e5-10s.csv` repeats it for ten seconds, and
`e5-10s-200us.csv` is the 200 us guard time of Section 5.5. The phase windows
of Table 9 are cut for the 2200 us guard time, so on the 200 us capture only
the mean current and the sleep floor are meaningful.

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
