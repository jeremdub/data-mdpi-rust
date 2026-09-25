#!/usr/bin/env python3
"""E1: timing determinism at the radio boundary (Table 5 of the paper).

Two boards on a 101-slot slotframe (1.01 s): the coordinator (NODE_ID 0)
beacons every 10 s, and the leaf (NODE_ID 1, PARENT = the coordinator) answers
with an acknowledged keep-alive, so each board both transmits and receives.
`tsch-ramp-trace` forks seven timer and radio events of a slot to header pins
through the PPI, with no CPU in the path, and a Saleae Logic Pro 16 records
them. One board is probed per capture, so the run is repeated once per role.
Each run of the paper is 3 h; the six-hour run of Table S2 was captured the
same way.

The serial log carries the synchronization line and, through
STATS_PERIOD_MS, a periodic neighbor-table dump; the determinism statistics
come from the analyzer export alone.

Edit the BENCH SETUP block below once (cargo project path, probe serials, the
coordinator's FICR address), then:
    python experiment.py --run 10800      # the run length of the paper
    python experiment.py                  # 20 min, to check the bench

Writes e1_sync.csv. Export the analyzer capture of the probed board as
<role>-logic_analyzer.csv next to the log; `check_e1.py` recomputes Table 5
from those exports.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import statistics
from pathlib import Path

from ioteapot import Node, setup_logging
from ioteapot.environments import Local
from ioteapot.environments.local import LocalDevice, Nrf802154Sniffer
from ioteapot.experiment import Experiment, Topology, UnitDiskRadioMedium
from ioteapot.experiment.event import (
    BaseEventListener,
    StartNodeAction,
    StopExperimentAction,
)
from ioteapot.os.embassy import Application as EmbassyApplication
from ioteapot.platforms import Nrf52840dkPlatform

# ===========================================================================
# BENCH SETUP -- static values for this bench; edit once.
# ===========================================================================

# The cargo project that contains src/bin/tsch-node.rs (see firmware/README.md).
NRF_EXAMPLE = "/home/jeremydub/vub/jeremy-develop/examples/nrf52840"
# NRF_EXAMPLE = "/Users/jeremydub/vub/dot15d4/jeremy-develop/examples/nrf52840"

# Physical nRF52840-DK boards, pinned by debug-probe serial (`nrfjprog --ids`).
# An *_ADDR is that board's FICR extended address: the 16-hex value it prints
# at boot as `node N: PARENT=<hex>` (top-level README, "Board bring-up").

ROOT_PROBE = "1366:1061:001050241502"  # leaf, arm A -- most negative
ROOT_ADDR = "3b8be9e6da5f2a83"

LEAF_PROBE = "1366:1061:001050262788"  # leaf, arm B -- most positive
LEAF_ADDR = "784b2f40045f43c5"

if False:
    ROOT_ADDR, LEAF_ADDR = LEAF_ADDR, ROOT_ADDR
    ROOT_PROBE, LEAF_PROBE = LEAF_PROBE, ROOT_PROBE


# Cargo features per role (see firmware/README.md).
FEATURES_ROOT = ["tsch-coordinator", "sync-error-log", "tsch-log", "defmt", "tsch-ramp-trace"]
FEATURES_LEAF = ["tsch", "sync-error-log", "tsch-log", "defmt", "tsch-ramp-trace"]


def _check_setup() -> None:
    for name, value in [
        ("ROOT_PROBE", ROOT_PROBE),
        ("ROOT_ADDR", ROOT_ADDR),
        ("LEAF_PROBE", LEAF_PROBE),
    ]:
        if "FILL_ME" in value:
            raise SystemExit(
                f"{name} is not set -- fill in the BENCH SETUP block at the top "
                f"of this file (see the top-level README, 'Board bring-up')."
            )
    if not Path(NRF_EXAMPLE, "Cargo.toml").exists():
        raise SystemExit(
            f"NRF_EXAMPLE={NRF_EXAMPLE!r} has no Cargo.toml; point it at "
            f"<repo>/examples/nrf52840."
        )


# ===========================================================================
# Listener: serial dumper + live per-node TSCH sync-error statistics.
# ===========================================================================

def _pct(sorted_vals, p):
    """Linear-interpolated percentile of a pre-sorted list."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


class SyncErrorListener(BaseEventListener):
    """Parse `tsch sync asn=<u64> src=frame|ack err_ns=<i64>` lines (positive =
    local clock ahead of the time source), keep running statistics per node,
    echo every serial line, and optionally dump the raw samples to CSV.
    Backend timestamps are milliseconds since the experiment started."""

    SYNC_RE = re.compile(r"tsch sync asn=(\d+) src=(\w+) err_ns=(-?\d+)")

    def __init__(self, skip_warmup: int = 30, csv_path: str | None = None) -> None:
        # The node's drift estimator is a moving average over the last
        # MAC_TSCH_DRIFT_BUFFER_LEN (8) observed drifts; statistics only mean
        # something once that buffer is full, so the first samples are dropped.
        self.skip_warmup = skip_warmup
        self.csv_path = csv_path
        # per-node state
        self._errs_us: dict[str, list[float]] = {}
        self._skipped: dict[str, int] = {}
        self._rows: list[tuple[float, str, int, str, int]] = []

    # callbacks

    def experiment_started(self, timestamp: float) -> None:
        print("  experiment started")

    def serial_message(self, node, timestamp, message, *, clocks=None) -> None:
        node = str(node)
        t_s = timestamp / 1000.0
        print(f"  [{t_s:8.4f}s] {node:<22} | {message}")

        m = self.SYNC_RE.search(str(message))
        if not m:
            return
        asn, src, err_ns = int(m.group(1)), m.group(2), int(m.group(3))

        if src == "ack":
            return

        self._rows.append((t_s, node, asn, src, err_ns))
        val_us = err_ns / 1000.0

        skipped = self._skipped.get(node, 0)
        if skipped < self.skip_warmup:
            self._skipped[node] = skipped + 1
            print(
                f"      >> {node} sync[{src}] {val_us:+9.3f} us | "
                f"warm-up {skipped + 1}/{self.skip_warmup}, ignored"
            )
            return

        errs = self._errs_us.setdefault(node, [])
        errs.append(val_us)
        if len(errs) % 10 == 0:
            self._print_stats(node, src, errs)

    def experiment_ended(self, timestamp: float) -> None:
        print("\n  === per-node sync-error summary ===")
        for node in sorted(self._errs_us):
            self._print_stats(node, "all", self._errs_us[node], final=True)
        if self.csv_path:
            self._write_csv()

    # helpers

    def _print_stats(self, node, src, errs, final: bool = False) -> None:
        a = sorted(abs(x) for x in errs)
        sd = statistics.pstdev(errs) if len(errs) > 1 else 0.0
        tag = "FINAL" if final else f"sync[{src}]"
        last = "" if final else f"{errs[-1]:+9.3f} us | "
        print(
            f"      >> {node} {tag} {last}n={len(errs)} "
            f"mean={statistics.fmean(errs):+7.3f} "
            f"median={statistics.median(errs):+7.3f} sd={sd:6.3f} "
            f"p95|.|={_pct(a, 0.95):6.3f} p99|.|={_pct(a, 0.99):6.3f} "
            f"spread={max(errs) - min(errs):6.3f} us"
        )

    def _write_csv(self) -> None:
        self._rows.sort()
        with open(self.csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(("time_s", "node", "asn", "src", "err_ns"))
            w.writerows(self._rows)
        print(f"  {len(self._rows)} sync samples -> {self.csv_path}")


# ===========================================================================
# Experiment.
# ===========================================================================

def build_experiment(run_s: float) -> tuple[Experiment, list]:
    topology = Topology(medium=UnitDiskRadioMedium(transmitting_range=100.0))

    # Root / time source. STATS_PERIOD_MS => periodic neighbor-table dump.
    root = Node(
        position=(0.0, 0.0, 0.0),
        identifier="root",
        platform=Nrf52840dkPlatform(),
        application=EmbassyApplication(
            app_folder=NRF_EXAMPLE,
            binary="tsch-node",
            features=list(FEATURES_ROOT),
            environment_variables={
                "NODE_ID": "0",
                "SCHEDULE": "chain",
                "STATS_PERIOD_MS": "120000",
            },
            keep_build_artifacts=True,
        ),
        auto_start=False,
    )

    # Leaf. PARENT is baked to the ROOT board's FICR address at compile time,
    # and the devices list below pins "root" to that same physical board
    # (ROOT_PROBE), so the parent role runs on the right hardware. Held until
    # the root is beaconing.
    leaf = Node(
        position=(1.0, 0.0, 0.0),
        identifier="leaf",
        platform=Nrf52840dkPlatform(),
        application=EmbassyApplication(
            app_folder=NRF_EXAMPLE,
            binary="tsch-node",
            features=list(FEATURES_LEAF),
            environment_variables={
                "NODE_ID": "1",
                "PARENT": ROOT_ADDR,
                "SCHEDULE": "chain",
                "STATS_PERIOD_MS": "120000",
                "BEACON": "0",
                "DOT15D4_MAC_TSCH_KEEP_ALIVE_PERIOD": "700",
            },
            keep_build_artifacts=True,
        ),
    )

    for n in (root, leaf):
        topology.add_node(n)

    # Node -> physical board pinning, by probe serial.
    devices = [
        (root, LocalDevice(probe=ROOT_PROBE)),
        (leaf, LocalDevice(probe=LEAF_PROBE)),
    ]

    exp = Experiment(topology=topology, name="e1_two_node_baseline")
    exp.after(3).do(StartNodeAction(node=root))
    exp.after(run_s).do(StopExperimentAction())
    return exp, devices


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=float, default=1200.0, help="run length (s)")
    args = ap.parse_args()

    _check_setup()
    setup_logging(logging.INFO)
    exp, devices = build_experiment(args.run)

    env = Local(
        devices=devices,
        flash_concurrency=len(devices),
        sniffers=Nrf802154Sniffer(channel=26, pcap_path="/tmp/test.pcap"),
    )
    exp.run(env, event_listener=SyncErrorListener(csv_path="e1_sync.csv"))


if __name__ == "__main__":
    main()
