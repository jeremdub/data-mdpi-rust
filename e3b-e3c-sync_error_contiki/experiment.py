#!/usr/bin/env python3
"""E3b and E3c: the Contiki-NG same-hardware baseline (the Contiki-NG rows of
Table 7 of the paper).

One coordinator and one leaf, both running Contiki-NG on the same nRF52840-DK
boards as dot15d4, on the 47-slot slotframe and 10 s beacon period of
`contiki-tsch-trace/`. The two nodes exchange only beacons, so synchronization
is frame-based throughout. E3b and E3c differ in the rtimer tick alone, which
is set when the Contiki-NG image is built, not here:

    make ... RTIMER_SECOND=62500     # E3b, the 16 us tick the nRF port ships
    make ... RTIMER_SECOND=1000000   # E3c, one tick per microsecond

Each role runs on either stack, so the four combinations are one command line
apart. Contiki-NG in both roles is E3b/E3c; a mixed pair is interoperability,
which `e4-interoperability/experiment.py` runs instead, because there the
joined node also sends data frames.

    ./experiment.py --coordinator contiki --leaf contiki --run 14400  # as in the paper
    ./experiment.py --coordinator rust    --leaf rust
    ./experiment.py --coordinator rust    --leaf contiki
    ./experiment.py --coordinator contiki --leaf rust

Needs CONTIKI in the environment, pointing at the patched Contiki-NG tree.
Export the analyzer capture as logic-analyzer.csv next to the log;
`check_e3.py` recomputes the Contiki-NG rows of Table 7.
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
from ioteapot.os import contiki
from ioteapot.os.embassy import Application as EmbassyApplication
from ioteapot.platforms import Nrf52840dkPlatform

# ===========================================================================
# BENCH SETUP -- static values for this bench
# ===========================================================================

# Cargo project holding src/bin/tsch/tsch-node.rs.
NRF_EXAMPLE = "/home/jeremydub/vub/jeremy-develop/examples/nrf52840"
# Contiki-NG application folder, with a "coordinator" and a "joining-node"
# target.
CONTIKI_APP = "./contiki-tsch-trace/app"

# Physical nRF52840-DK boards, pinned by debug-probe serial (`nrfjprog --ids`)
ROOT_PROBE = "1366:1061:001050241502"
ROOT_PORT = "/dev/ttyACM10"
ROOT_ADDR = "3b8be9e6da5f2a83"

LEAF_PROBE = "1366:1061:001050262788"
LEAF_PORT = "/dev/ttyACM16"

PAN_ID = "0xabcd"
CHANNEL = 26

# Cargo features per role.
FEATURES_COORDINATOR = [
    "tsch-coordinator",
    "sync-error-log",
    "tsch-log",
    "defmt",
    "radio-trace",
]
FEATURES_LEAF = ["tsch", "sync-error-log", "tsch-log", "defmt", "radio-trace"]

# Cell layout of the Rust nodes (see src/bin/tsch/schedule.rs). "learn" makes the
# leaf copy the slotframe and the links from the first Enhanced Beacon it
# hears, which is what it needs against a Contiki-NG coordinator. A static
# layout such as "chain" needs ROOT_ADDR instead.
COORDINATOR_SCHEDULE = "chain"
LEAF_SCHEDULE = "learn"

# Neighbor-table dump period, in milliseconds; 0 disables it.
STATS_PERIOD_MS = "120000"


def _check_setup(coordinator: str, leaf: str) -> None:
    if "rust" in (coordinator, leaf) and not Path(NRF_EXAMPLE, "Cargo.toml").exists():
        raise SystemExit(
            f"NRF_EXAMPLE={NRF_EXAMPLE!r} has no Cargo.toml; point it at "
            f"<repo>/examples/nrf52840."
        )
    if "contiki" in (coordinator, leaf) and not Path(CONTIKI_APP).is_dir():
        raise SystemExit(f"CONTIKI_APP={CONTIKI_APP!r} is not a directory.")
    if leaf == "rust" and LEAF_SCHEDULE != "learn" and "FILL_ME" in ROOT_ADDR:
        raise SystemExit(
            f"SCHEDULE={LEAF_SCHEDULE} needs ROOT_ADDR -- fill in the BENCH "
            f"SETUP block, or use LEAF_SCHEDULE='learn'."
        )


def coordinator_application(stack: str):
    """Firmware for the coordinator (time root) on `stack`."""
    if stack == "contiki":
        return contiki.Application(app_folder=CONTIKI_APP, target="coordinator")
    return EmbassyApplication(
        app_folder=NRF_EXAMPLE,
        binary="tsch-node",
        features=list(FEATURES_COORDINATOR),
        environment_variables={
            "NODE_ID": "0",
            "SCHEDULE": COORDINATOR_SCHEDULE,
            "STATS_PERIOD_MS": STATS_PERIOD_MS,
            "DOT15D4_MAC_PAN_ID": PAN_ID,
        },
        keep_build_artifacts=True,
    )


def leaf_application(stack: str):
    """Firmware for the leaf on `stack`."""
    if stack == "contiki":
        return contiki.Application(
            app_folder=CONTIKI_APP,
            target="joining-node",
            environment_variables={"SENDER": "0"},
        )
    env = {
        "NODE_ID": "1",
        "SCHEDULE": LEAF_SCHEDULE,
        "BEACON": "0",
        "STATS_PERIOD_MS": STATS_PERIOD_MS,
        "DOT15D4_MAC_PAN_ID": PAN_ID,
        "SENDER": "1",
        "SEND_PERIOD_MS": "30000",
        "BEACON_JITTER_MS": "2000",
    }
    # A static layout follows one fixed parent; "learn" accepts any coordinator.
    if LEAF_SCHEDULE != "learn":
        env["PARENT"] = ROOT_ADDR
    return EmbassyApplication(
        app_folder=NRF_EXAMPLE,
        binary="tsch-node",
        features=list(FEATURES_LEAF),
        environment_variables=env,
        keep_build_artifacts=True,
    )


# ===========================================================================
# Listener: serial dumper + the numbers of the interoperability table.
# ===========================================================================

# The line each stack prints for one event of the table. `eb_tx` and `data_rx`
# are read on the coordinator, the others on the joined node.
EVENTS = {
    "rust": {
        "eb_tx": r"tsch beacon sent",
        "data_rx": r"tsch rx asn=\d+ type=data",
        "boot": r"val boot",
        "join": r"val join ok",
        "desync": r"val desync",
        "data_tx": r"val tx seq=",
    },
    "contiki": {
        "eb_tx": r"packet sent to 0000\.0000\.0000\.0000",
        "data_rx": r"received from \S+ with seqno",
        "boot": r"Starting Contiki-NG",
        "join": r"association done",
        "desync": r"leaving the network",
        "data_tx": r"\] sending \d+ to",
    },
}
COORDINATOR_EVENTS = ("eb_tx", "data_rx")
LEAF_EVENTS = ("boot", "join", "desync", "data_tx")

# Transmissions the joined node spent on one data frame, acknowledgment
# included. One means no retransmission.
ATTEMPTS = {
    "rust": re.compile(r"tsch tx sent asn=\d+ shared=\w+ attempts=(\d+)"),
    "contiki": re.compile(
        r"packet sent to (?!0000\.0000\.0000\.0000)\S+, seqno \d+, status \d+, tx (\d+)"
    ),
}


def _ratio(count: int, total: int) -> str:
    return f"{count}/{total} ({100.0 * count / total:.1f}%)" if total else "--"


class InteropListener(BaseEventListener):
    """Echo every serial line, count the events behind the interoperability
    table, and dump the raw sync samples to CSV.

    Both stacks print `tsch sync asn=<u64> src=frame|ack err_ns=<i64>`, where
    `src=frame` is the error the node measured on a frame from its time source
    and `src=ack` the Time Correction IE it read from an Enhanced ACK. A
    positive error means the local clock is ahead of the time source. Backend
    timestamps are milliseconds since the experiment started."""

    SYNC_RE = re.compile(r"tsch sync asn=(\d+) src=(\w+) err_ns=(-?\d+)")

    def __init__(
        self,
        coordinator: str,
        leaf: str,
        skip_warmup: int = 30,
        csv_path: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.leaf = leaf
        # The drift estimator is a moving average over the last
        # MAC_TSCH_DRIFT_BUFFER_LEN (8) drifts, so the statistics only mean
        # something once that buffer is full. Drop the first samples.
        self.skip_warmup = skip_warmup
        self.csv_path = csv_path
        self.counts = dict.fromkeys(
            ("eb_tx", "eb_rx", "data_tx", "data_rx", "ack", "retx", "desync"), 0
        )
        self.joined = False
        self.boot_s = 0.0
        self.join_s: float | None = None
        self.end_s = 0.0
        self.skipped = 0
        self.errs_us: list[float] = []
        self.rows: list[tuple[float, str, int, str, int]] = []

    # callbacks

    def experiment_started(self, timestamp: float) -> None:
        print("  experiment started")

    def radio_packet(self, timestamp, data) -> None:
        # Trim long payloads for readability; pcap has the full bytes.
        hex_preview = data.hex(" ")
        print(f"  [{timestamp:6.4f} ms] radio    | {len(data):3d}B :  {hex_preview}")

    def serial_message(self, node, timestamp, message, *, clocks=None) -> None:
        node, message = str(node), str(message)
        t_s = timestamp / 1000.0
        print(f"  [{t_s:8.4f}s] {node:<22} | {message}")
        self.end_s = t_s

        if node == "root":
            stack, events = self.coordinator, COORDINATOR_EVENTS
        else:
            stack, events = self.leaf, LEAF_EVENTS
        for event in events:
            if re.search(EVENTS[stack][event], message):
                self._count(event, t_s)

        if not self.joined or node != "leaf":
            return
        m = ATTEMPTS[self.leaf].search(message)
        if m:
            self.counts["retx"] += int(m.group(1)) - 1
        m = self.SYNC_RE.search(message)
        if not m:
            return
        asn, src, err_ns = int(m.group(1)), m.group(2), int(m.group(3))
        self.rows.append((t_s, node, asn, src, err_ns))
        if src == "ack":
            # One Enhanced ACK received, and its Time Correction IE parsed.
            self.counts["ack"] += 1
        elif src == "frame":
            # One beacon received from the time source, and the error it gave.
            self.counts["eb_rx"] += 1
            if self.skipped < self.skip_warmup:
                self.skipped += 1
            else:
                self.errs_us.append(err_ns / 1000.0)

    def experiment_ended(self, timestamp: float) -> None:
        c = self.counts
        errs = self.errs_us
        join = "--" if self.join_s is None else f"{self.join_s:.2f} s"
        err = "--" if not errs else f"{statistics.fmean(map(abs, errs)):.3f} us"
        print(
            f"\n  === E4: {self.coordinator} coordinator, {self.leaf} joined node ==="
        )
        print(f"  join       {join}")
        print(f"  run        {self.end_s / 3600.0:.2f} h")
        print(f"  desync     {c['desync']}")
        print(f"  EB RR      {_ratio(c['eb_rx'], c['eb_tx'])}")
        print(f"  PDR        {_ratio(c['data_rx'], c['data_tx'])}")
        print(f"  Enh-ACK    {_ratio(c['ack'], c['data_rx'])}")
        print(f"  retransmit {_ratio(c['retx'], c['data_tx'])}")
        print(f"  mean |err| {err} (n={len(errs)})")
        if self.csv_path:
            self._write_csv()

    # helpers

    def _count(self, event: str, t_s: float) -> None:
        if event == "boot":
            # The node can reset before the run proper starts, so the last
            # boot before association is the one the join time is measured on.
            if not self.joined:
                self.boot_s = t_s
        elif event == "join":
            self.joined = True
            if self.join_s is None:
                self.join_s = t_s - self.boot_s
        elif event == "desync":
            self.joined = False
            self.counts["desync"] += 1
        elif self.joined:
            # Nothing counts outside the network: the serial line still carries
            # bytes buffered by the previous run, and a beacon sent while the
            # node is not associated is not a missed reception.
            self.counts[event] += 1

    def _write_csv(self) -> None:
        self.rows.sort()
        with open(self.csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(("time_s", "node", "asn", "src", "err_ns"))
            w.writerows(self.rows)
        print(f"  {len(self.rows)} sync samples -> {self.csv_path}")


# ===========================================================================
# Experiment.
# ===========================================================================


def build_experiment(
    coordinator: str, leaf: str, run_s: float
) -> tuple[Experiment, list]:
    """Two nodes, one per role, each on the requested stack."""
    topology = Topology(medium=UnitDiskRadioMedium(transmitting_range=100.0))

    # Started at t = 3 s, so the leaf is already scanning when the first
    # Enhanced Beacon goes out.
    root = Node(
        position=(0.0, 0.0, 0.0),
        identifier="root",
        platform=Nrf52840dkPlatform(),
        application=coordinator_application(coordinator),
        auto_start=False,
    )
    leaf_node = Node(
        position=(1.0, 0.0, 0.0),
        identifier="leaf",
        platform=Nrf52840dkPlatform(),
        application=leaf_application(leaf),
    )

    for n in (root, leaf_node):
        topology.add_node(n)

    # Node -> physical board pinning, by probe serial.
    devices = [
        (root, LocalDevice(probe=ROOT_PROBE, port=ROOT_PORT)),
        (leaf_node, LocalDevice(probe=LEAF_PROBE, port=LEAF_PORT)),
    ]

    exp = Experiment(topology=topology, name=run_name(coordinator, leaf))
    exp.after(3).do(StartNodeAction(node=root))
    exp.after(run_s).do(StopExperimentAction())
    return exp, devices


def run_name(coordinator: str, leaf: str) -> str:
    return f"interop_{coordinator}-coord_{leaf}-leaf"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coordinator", choices=("rust", "contiki"), default="rust")
    ap.add_argument("--leaf", choices=("rust", "contiki"), default="contiki")
    ap.add_argument("--run", type=float, default=1200.0, help="run length (s)")
    ap.add_argument("--out", default=".", help="directory for the CSV and pcap")
    args = ap.parse_args()

    _check_setup(args.coordinator, args.leaf)
    setup_logging(logging.INFO)
    exp, devices = build_experiment(args.coordinator, args.leaf, args.run)

    name = run_name(args.coordinator, args.leaf)
    env = Local(
        devices=devices,
        flash_concurrency=len(devices),
        sniffers=Nrf802154Sniffer(channel=26, pcap_path="/tmp/test.pcap"),
    )
    listener = InteropListener(
        args.coordinator, args.leaf, csv_path=str(Path(args.out, f"{name}.csv"))
    )
    exp.run(env, event_listener=listener)


if __name__ == "__main__":
    main()
