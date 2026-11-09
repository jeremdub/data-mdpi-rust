#!/usr/bin/env python3
"""
Energy
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

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

# === BENCH SETUP ===========================================================

NRF_EXAMPLE = "/Users/jeremydub/vub/dot15d4/jeremy-develop/examples/nrf52840"

# Boards in role order (root, n1, n2), pinned by debug-probe serial.
probes = [
    "1366:1061:001050231130",
    "1366:1061:001050282182",
]
addresses = [
    "b965b0a94760eb60",
    "74eb017427e304d2",
]

# === NETWORK ===============================================================

CHANNEL = 20             # single channel, so one parked sniffer sees every frame
BEACON_PERIOD_S = 10
BEACON_JITTER_MS = 500   # keeps neighbours from beaconing in lockstep
KEEP_ALIVE_SLOTS = 400   # macKeepAlivePeriod, in 10 ms timeslots

V1_SEND_PERIOD_MS = 30_000
V1_SEND_JITTER_MS = 0

V2_SLOTFRAME = 8         # 8 slots = 80 ms per cell
V2_SEND_PERIOD_MS = 250
V2_SEND_JITTER_MS = 100
V2_STATS_PERIOD_MS = 60_000   # root neighbour-table dump
V2_FIRST_START_S = 20
JOIN_STAGGER_S = int(2.5 * BEACON_PERIOD_S)

FEATURES = ["tsch", "tsch-log", "sync-error-log", "defmt"]
FEATURES_ROOT = FEATURES + ["tsch-coordinator"]
FEATURES_RELAY = FEATURES + ["tsch-coordinator", "tsch-association"]
FEATURES_LEAF = FEATURES + ["tsch-association"]

DEFAULT_RUN_S = 3600.0


def node_specs(jitter_ms: int | None = None) -> list[dict]:
    """One entry per board: id, cargo features, build env, start second."""
    common = {"CHANNEL": str(CHANNEL), "BEACON_PERIOD_S": str(BEACON_PERIOD_S)}

    def sender(period_ms: int, default_jitter: int) -> dict:
        j = default_jitter if jitter_ms is None else jitter_ms
        return {"SENDER": "1", "SEND_PERIOD_MS": str(period_ms),
                "SEND_JITTER_MS": str(j)}

    def spec(node_id, features, start_s, **env) -> dict:
        return {"id": node_id, "features": features, "start_s": start_s,
                "env": {**common, **env}}

    base = {"DOT15D4_MAC_TSCH_KEEP_ALIVE_PERIOD": str(KEEP_ALIVE_SLOTS),
            "STATS_PERIOD_MS": "600000"}
    jit = {"BEACON_JITTER_MS": str(BEACON_JITTER_MS)}
    tx = sender(V1_SEND_PERIOD_MS, V1_SEND_JITTER_MS)

    return [
        spec("root", FEATURES_ROOT, 0,
             **base, **jit, NODE_ID="0", SCHEDULE="star"),
        # Learns its cells from the root's beacon, relays for node 2.
        spec("n1", FEATURES_RELAY, 30, **base, **jit, **tx,
             NODE_ID="1", SCHEDULE="star", PARENT=addresses[0]),
        # Joins through node 1, so this join runs over relayed beacons.
    ]

SLOT_S = 0.010
# Discarded after each node's first correction. By time, not by sample count:
# the drift estimator converges at the rate corrections arrive, which differs by
# an order of magnitude between a sender and a listener.
SYNC_WARMUP_S = 30.0

class Listener(BaseEventListener):
    def __init__(self) -> None:
        pass


    def experiment_started(self, timestamp: float) -> None:
        pass

    def radio_packet(self, timestamp, data) -> None:
        tail = " ..." if len(data) > 16 else ""
        print(f"  [{timestamp / 1000.0:8.2f}s] sniffer  | "
              f"{len(data):3d}B  {data[:16].hex(' ')}{tail}")

    def serial_message(self, node, timestamp, message, *, clocks=None) -> None:
        node, message, t_s = str(node), str(message), timestamp / 1000.0
        print(f"  [{t_s:8.2f}s] {node:<8} | {message}")

    def experiment_ended(self, timestamp: float) -> None:
        pass

# === EXPERIMENT ============================================================

def build_experiment(run_s: float, jitter_ms: int | None = None):
    specs = node_specs(jitter_ms)
    assert len(specs) <= len(probes), "not enough boards in the BENCH SETUP block"

    # Every board hears every other one; the topology comes from the cells and
    # from PARENT, not from the positions.
    topology = Topology(
        medium=UnitDiskRadioMedium(transmitting_range=120.0, interference_range=150.0)
    )
    devices, starts = [], []
    for index, spec in enumerate(specs):
        node = Node(
            position=(index * 100.0, 0.0, 0.0),
            identifier=spec["id"],
            platform=Nrf52840dkPlatform(),
            auto_start=False,
            application=EmbassyApplication(
                app_folder=NRF_EXAMPLE,
                binary="tsch-node",
                features=list(spec["features"]),
                environment_variables=dict(spec["env"]),
                keep_build_artifacts=True,
            ),
        )
        topology.add_node(node)
        devices.append((node, LocalDevice(probe=probes[index])))
        starts.append((spec["start_s"], node))

    exp = Experiment(topology=topology, name="Energy")
    for start_s, node in starts:
        exp.after(start_s).do(StartNodeAction(node=node))
    exp.after(run_s).do(StopExperimentAction())
    return exp, devices, specs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=float, default=None, help="run length (s)")
    ap.add_argument("--pcap", default=None, help="pcap path (default /tmp/trace.pcap)")
    ap.add_argument("--send-jitter", type=int, default=None, metavar="MS",
                    help="override the +/- send jitter of every sender, in ms")
    args = ap.parse_args()

    run_s = args.run if args.run is not None else DEFAULT_RUN_S
    setup_logging(logging.DEBUG)
    exp, devices, specs = build_experiment(run_s, args.send_jitter)

    jitter = sorted({s["env"]["SEND_JITTER_MS"] for s in specs if "SEND_JITTER_MS" in s["env"]})
    print(f"  send jitter: +/-{', '.join(jitter)} ms")
    settle_s = max(s["start_s"] for s in specs) + 3 * BEACON_PERIOD_S
    if run_s < settle_s + 60:
        print(f"  WARNING: only {max(run_s - settle_s, 0):.0f}s of steady state; "
              f"this is a smoke test, not a validation run")

    env = Local(
        devices=devices,
        flash_concurrency=len(devices),
        sniffers=Nrf802154Sniffer(channel=CHANNEL,
                                  pcap_path=args.pcap or f"./trace.pcap"),
    )
    exp.run(env, event_listener=Listener())


if __name__ == "__main__":
    main()
