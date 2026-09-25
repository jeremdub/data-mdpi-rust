#!/usr/bin/env python3
"""E3a: dot15d4 on the slotframe of the Contiki-NG baseline (the dot15d4 rows
of Table 7 of the paper).

The star of E2, one root (NODE_ID 0) and ten leaves, moved to the 47-slot
slotframe and 10 s beacon period that E3b and E3c run, so the two stacks are
compared on the same schedule. Measurement is unchanged: every leaf logs its
own synchronization error, and the probed leaf and the root are wired to a
Saleae Logic Pro 16 through `radio-trace`.

Only SLOTFRAME_SIZE separates this script from e2-sync_error/experiment.py.

Variants (``--variant``):
    staggered     children start 10 s apart          (default)
    simultaneous  children start together             (used for the paper)

Edit the BENCH SETUP block below once (cargo project path, probe serials, the
root's FICR address), then:
    python experiment.py --run 14400 --variant simultaneous   # as in the paper
    python experiment.py                                      # 15 min, to check the bench

Writes e2_<variant>_sync.csv and trace.pcap. Export the analyzer capture as
logic-analyzer.csv next to the log; `check_e2.py` reads this folder the same
way as E2.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import statistics

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
# BENCH SETUP -- static values for this bench
# ===========================================================================

# The cargo project that contains src/bin/tsch-node.rs (see firmware/README.md).
NRF_EXAMPLE = "/home/jeremydub/vub/jeremy-develop/examples/nrf52840"
# NRF_EXAMPLE = "/Users/jeremydub/vub/dot15d4/jeremy-develop/examples/nrf52840"

# Physical nRF52840-DK boards, pinned by debug-probe serial (`nrfjprog --ids`).
ROOT_PROBE = "1366:1061:001050241502"  # leaf, arm A -- most negative
ROOT_ADDR = "3b8be9e6da5f2a83"

CHILDREN_PROBES = [
    "1366:1061:001050213055",  # child 1 (Node ID=1)
    "1366:1061:001050231130",  # child 2 (Node ID=2)
    "1366:1061:001050285558",  # child 3 (Node ID=3)
    "1366:1061:001050295563",  # child 4 (Node ID=4)
    "1366:1061:001050247109",  # child 5 (Node ID=5)
    "1366:1061:001050289517",  # child 6 (Node ID=6)
    "1366:1061:001050200402",  # child 7 (Node ID=7)
    "1366:1061:001050246808",  # child 8 (Node ID=8)
    "1366:1061:001050282182",  # child 9 (Node ID=9)
    "1366:1061:001050262788",  # child 10 (Node ID=10)
]

# Cargo features per role (see firmware/README.md).
FEATURES_ROOT = [
    "tsch-coordinator",
    "sync-error-log",
    "tsch-log",
    "defmt",
    "radio-trace",
]
FEATURES_LEAF = ["tsch", "sync-error-log", "tsch-log", "tsch", "defmt", "radio-trace"]
# FEATURES_LEAF = ["tsch", "sync-error-log", "tsch-log", "tsch", "defmt"]

# Slotframe length, in timeslots. 47 to match the Contiki-NG baseline of E3b
# and E3c, which follows Elsts et al., LCN 2016, Sec. VI.
SLOTFRAME_SIZE = 47


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

    def __init__(self, skip_warmup: int = 60, csv_path: str | None = None) -> None:
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

    def radio_packet(self, timestamp, data) -> None:
        # Trim long payloads for readability; pcap has the full bytes.
        hex_preview = data[:16].hex(" ")
        suffix = " ..." if len(data) > 16 else ""
        print(
            f"  [{timestamp:6.4f} ms] radio    | {len(data):3d}B  {hex_preview}{suffix}"
        )

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


def build_experiment(variant: str, run_s: float) -> tuple[Experiment, list]:
    simultaneous = variant == "simultaneous"

    topology = Topology(medium=UnitDiskRadioMedium(transmitting_range=100.0))

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
                "SCHEDULE": "star",
                # The 47-slot slotframe of the Contiki-NG baseline, so E3a and
                # E3b/E3c share one schedule. "star" alone is 101 slots.
                "SLOTFRAME_SIZE": f"{SLOTFRAME_SIZE}",
                "STATS_PERIOD_MS": "20000",
                "DOT15D4_MAC_TSCH_MAX_LINKS": f"{len(CHILDREN_PROBES) + 3}",
            },
            keep_build_artifacts=True,
        ),
    )
    topology.add_node(root)

    # Children arranged around the root, all inside the transmitting range.
    # Every child's PARENT is baked to the ROOT board's address; the devices
    # list below pins "root" to that same physical board (ROOT_PROBE).
    # BEACON=0: pure leaf, so its only Tx cell is its dedicated up cell
    # -> clean association and keep-alive on that slot.
    devices = [(root, LocalDevice(probe=ROOT_PROBE))]
    for i, probe in enumerate(CHILDREN_PROBES):
        child = Node(
            position=(i * 20.0, 0.0, 0.0),
            identifier=f"child{i + 1}",
            platform=Nrf52840dkPlatform(),
            auto_start=simultaneous,
            application=EmbassyApplication(
                app_folder=NRF_EXAMPLE,
                binary="tsch-node",
                features=list(FEATURES_LEAF),
                environment_variables={
                    "NODE_ID": f"{i + 1}",
                    "PARENT": ROOT_ADDR,
                    "SCHEDULE": "star",
                    "SLOTFRAME_SIZE": f"{SLOTFRAME_SIZE}",
                    "BEACON": "0",
                },
                keep_build_artifacts=True,
            ),
        )
        topology.add_node(child)
        devices.append((child, LocalDevice(probe=probe)))

    exp = Experiment(topology=topology, name=f"e2_star_{variant}")
    if not simultaneous:
        for i, child in enumerate(topology.nodes[1:]):
            exp.after(i * 10 + 5).do(StartNodeAction(node=child))
    exp.after(run_s).do(StopExperimentAction())
    return exp, devices


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--variant",
        choices=["staggered", "simultaneous"],
        default="staggered",
    )
    ap.add_argument("--run", type=float, default=900.0, help="run length (s)")
    args = ap.parse_args()

    setup_logging(logging.INFO)
    exp, devices = build_experiment(args.variant, args.run)

    env = Local(
        devices=devices,
        flash_concurrency=len(devices),
        sniffers=Nrf802154Sniffer(channel=26, pcap_path="./trace.pcap"),
    )
    exp.run(
        env, event_listener=SyncErrorListener(csv_path=f"e2_{args.variant}_sync.csv")
    )


if __name__ == "__main__":
    main()
