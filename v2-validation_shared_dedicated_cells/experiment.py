#!/usr/bin/env python3
"""V1, V2a and V2b: functional validation of the dot15d4 TSCH MAC procedures
(Table 4 of the paper).

Three boards: N1 is the coordinator and time source, N2 and N3 join in that
order. `--variant` selects the arm.

    v1            Line from N3 to N2 to N1, on the minimal 6TiSCH schedule.
                  Each joining node walks the whole entry chain (scan, join,
                  associate) and then holds it: beacons, keep-alives and slow
                  acknowledged data. N3 joins on beacons relayed by N2, so the
                  second join runs at two hops. Default run 1 h.
    v2-dedicated  V2a: star, one dedicated transmit cell per child. The
                  reference arm of V2. Default run 30 min.
    v2-shared     V2b: star, both children in one shared cell, so their frames
                  meet and the TSCH CSMA-CA backoff runs. Default run 30 min.

The two arms of V2 differ only in the cell layout. The folders
`v1-validation_procedures` and `v2-validation_shared_dedicated_cells` hold the
same script.

Edit the BENCH SETUP block below, then:
    python experiment.py --variant v1
    python experiment.py --variant v2-shared --run 1800 --send-jitter 100

Writes <variant>_{tx,rx,sync}.csv and prints a per-node summary plus one line
per validation check. `check_v.py` recomputes Table 4 from the saved logs.
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

NRF_EXAMPLE = "/home/jeremydub/vub/jeremy-develop/examples/nrf52840"

# Boards in role order (root, n1, n2), pinned by debug-probe serial.
probes = [
    "1366:1061:001050295563",
    "1366:1061:001050241502",
    "1366:1061:001050285558",
]
# Matching FICR extended addresses, as printed at boot (`val boot ... addr=`).
addresses = [
    "88c4b2aeee4fae4e",
    "3b8be9e6da5f2a83",
    "5e9b0d5f1ee770d0",
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

DEFAULT_RUN_S = {"v1": 3600.0, "v2-shared": 1800.0, "v2-dedicated": 1800.0}


def node_specs(variant: str, jitter_ms: int | None = None) -> list[dict]:
    """One entry per board: id, cargo features, build env, start second."""
    common = {"CHANNEL": str(CHANNEL), "BEACON_PERIOD_S": str(BEACON_PERIOD_S)}

    def sender(period_ms: int, default_jitter: int) -> dict:
        j = default_jitter if jitter_ms is None else jitter_ms
        return {"SENDER": "1", "SEND_PERIOD_MS": str(period_ms),
                "SEND_JITTER_MS": str(j)}

    def spec(node_id, features, start_s, **env) -> dict:
        return {"id": node_id, "features": features, "start_s": start_s,
                "env": {**common, **env}}

    if variant == "v1":
        base = {"DOT15D4_MAC_TSCH_KEEP_ALIVE_PERIOD": str(KEEP_ALIVE_SLOTS),
                "STATS_PERIOD_MS": "600000"}
        jit = {"BEACON_JITTER_MS": str(BEACON_JITTER_MS)}
        tx = sender(V1_SEND_PERIOD_MS, V1_SEND_JITTER_MS)
        return [
            spec("root", FEATURES_ROOT, 0,
                 **base, **jit, NODE_ID="0", SCHEDULE="minimal"),
            # Learns its cells from the root's beacon, relays for node 2.
            spec("n1", FEATURES_RELAY, 30, **base, **jit, **tx,
                 NODE_ID="1", SCHEDULE="learn", PARENT=addresses[0]),
            # Joins through node 1, so this join runs over relayed beacons.
            spec("n2", FEATURES_LEAF, 90, **base, **tx,
                 NODE_ID="2", SCHEDULE="learn", PARENT=addresses[1], BEACON="0"),
        ]

    shape = {"SCHEDULE": "contention" if variant == "v2-shared" else "star",
             "SLOTFRAME_SIZE": str(V2_SLOTFRAME)}
    tx = sender(V2_SEND_PERIOD_MS, V2_SEND_JITTER_MS)
    return [
        spec("root", FEATURES_ROOT, 0, **shape, NODE_ID="0", NODE_COUNT="3",
             STATS_PERIOD_MS=str(V2_STATS_PERIOD_MS)),
        *(spec(f"n{i}", FEATURES_LEAF, V2_FIRST_START_S + JOIN_STAGGER_S * (i - 1),
               **shape, **tx, NODE_ID=str(i), PARENT=addresses[0], BEACON="0")
          for i in (1, 2)),
    ]


# === LOG PARSING ===========================================================

PATTERNS = {
    "boot": r"val boot node=(\d+) role=(\w+) addr=([0-9a-f]{16})",
    "pan": r"val pan started ms=(\d+)",
    "scan": r"val scan done found=(\d+) ms=(\d+)",
    "join": r"val join ok parent=([0-9a-f]{16}) pan=([0-9a-f]+) ms=(\d+)",
    "join_retry": r"val join retry ms=(\d+)",
    "assoc": r"val assoc ok short=([0-9a-f]+) ms=(\d+)",
    "assoc_fail": r"val assoc fail ms=(\d+)",
    "assoc_peer": r"val assoc peer dev=([0-9a-f]{16}) short=([0-9a-f]+) ms=(\d+)",
    "neighbor": (r"val neighbor index=\d+ addr=([0-9a-f]{16}) short=[0-9a-f]+ "
                 r"join_metric=\d+ rx=(\d+) tx_acked=(\d+) tx_attempts=(\d+) "
                 r"etx_q7=(\d+) lqi=(\d+)"),
    "tx": r"val tx seq=(\d+) ok=(\d) ms=(\d+)",
    "rx": r"val rx seq=(\d+) src=([0-9a-f]{16}) ms=(\d+)",
    # src=frame is ns-resolution; src=ack is whole microseconds (the Time
    # Correction IE); src=peer is measured for a non-time-source and applied to
    # nothing, which on the coordinator makes it an independent instrument.
    "sync": r"tsch sync asn=(\d+) src=(\w+) err_ns=(-?\d+)(?: peer=([0-9a-f]{16}))?",
    "beacon": r"tsch beacon sent asn=(\d+)",
    "keepalive": r"tsch keep-alive sent asn=(\d+) attempts=(\d+)",
    "tx_sent": r"tsch tx sent asn=(\d+) shared=(\w+) attempts=(\d+)",
    "tx_slot": r"tsch tx-slot asn=(\d+) sf=\d+ ch=\d+ shared=(\w+)",
    "tx_noack": r"tsch tx no-ack asn=(\d+)",
    "tx_drop": r"tsch tx drop asn=(\d+) nb=(\d+) max=(\d+) result=([\w-]+)",
    # `type=` is absent on firmware older than the drop-cause split.
    "rx_drop": r"tsch rx dropped asn=(\d+)(?: type=(\w+))? cause=([\w-]+)",
}
PATTERNS = {k: re.compile(v) for k, v in PATTERNS.items()}

SLOT_S = 0.010
# Discarded after each node's first correction. By time, not by sample count:
# the drift estimator converges at the rate corrections arrive, which differs by
# an order of magnitude between a sender and a listener.
SYNC_WARMUP_S = 30.0


@dataclass
class NodeState:
    role: str | None = None
    address: str | None = None
    pan_started_ms: int | None = None
    scans: int = 0
    scan_found: int = 0
    join_ms: int | None = None
    join_parent: str | None = None
    join_retries: int = 0
    assoc_short: str | None = None
    assoc_ms: int | None = None
    assoc_failed: int = 0
    assoc_peers: list = field(default_factory=list)        # (device, short, ms)
    neighbors: dict = field(default_factory=lambda: defaultdict(list))
    beacon_asns: list = field(default_factory=list)
    keepalive_asns: list = field(default_factory=list)
    keepalive_attempts: list = field(default_factory=list)
    tx_attempts: list = field(default_factory=list)        # per delivered frame
    tx_shared: int = 0
    tx_drops: list = field(default_factory=list)           # result strings
    tx_shared_asns: set = field(default_factory=set)
    tx_noack_asns: list = field(default_factory=list)
    tx_ok: int = 0
    tx_fail: int = 0
    rx: dict = field(default_factory=lambda: defaultdict(list))   # src -> [seq]
    sync_frame: list = field(default_factory=list)         # [(t_s, err_ns)]
    sync_ack: list = field(default_factory=list)
    sync_peer: dict = field(default_factory=lambda: defaultdict(list))
    sync_times_s: list = field(default_factory=list)       # for the gap check
    joined_at_s: float | None = None
    rx_drops: dict = field(default_factory=lambda: defaultdict(int))  # (type, cause)
    pending: tuple | None = None    # `tsch tx sent` awaiting its `val tx`


class ValidationListener(BaseEventListener):
    def __init__(self, variant: str, specs: list[dict], echo: bool = True) -> None:
        self.variant, self.echo = variant, echo
        # Score against what was configured, not against what reported in: a
        # node that joins and then hangs still emits sync lines.
        self.expected = {s["id"] for s in specs}
        self.joiners = {s["id"] for s in specs if s["id"] != "root"}
        self.senders = {s["id"] for s in specs if s["env"].get("SENDER") == "1"}
        self.nodes: dict[str, NodeState] = defaultdict(NodeState)
        self.tx_rows: list[tuple] = []
        self.rx_rows: list[tuple] = []
        self.sync_rows: list[tuple] = []

    # callbacks

    def experiment_started(self, timestamp: float) -> None:
        print(f"  {self.variant} started")

    def radio_packet(self, timestamp, data) -> None:
        if self.echo:
            tail = " ..." if len(data) > 16 else ""
            print(f"  [{timestamp / 1000.0:8.2f}s] sniffer  | "
                  f"{len(data):3d}B  {data[:16].hex(' ')}{tail}")

    def serial_message(self, node, timestamp, message, *, clocks=None) -> None:
        node, message, t_s = str(node), str(message), timestamp / 1000.0
        if self.echo:
            print(f"  [{t_s:8.2f}s] {node:<8} | {message}")
        for kind, pattern in PATTERNS.items():
            m = pattern.search(message)
            if m:
                self._record(self.nodes[node], node, kind, m, t_s)
                return

    def experiment_ended(self, timestamp: float) -> None:
        self._write_csvs()
        self._print_summary()

    # parsing

    def _record(self, s: NodeState, node: str, kind: str, m, t_s: float) -> None:
        if kind == "boot":
            s.role, s.address = m.group(2), m.group(3)
        elif kind == "pan":
            s.pan_started_ms = int(m.group(1))
        elif kind == "scan":
            s.scans += 1
            s.scan_found += int(m.group(1))
        elif kind == "join" and s.join_ms is None:
            s.join_parent, s.join_ms = m.group(1), int(m.group(3))
        elif kind == "join_retry":
            s.join_retries += 1
        elif kind == "assoc":
            s.assoc_short, s.assoc_ms = m.group(1), int(m.group(2))
        elif kind == "assoc_fail":
            s.assoc_failed += 1
        elif kind == "assoc_peer":
            s.assoc_peers.append((m.group(1), m.group(2), int(m.group(3))))
        elif kind == "neighbor":
            s.neighbors[m.group(1)].append(tuple(int(m.group(i)) for i in range(2, 7)))
        elif kind == "beacon":
            s.beacon_asns.append(int(m.group(1)))
        elif kind == "keepalive":
            s.keepalive_asns.append(int(m.group(1)))
            s.keepalive_attempts.append(int(m.group(2)))
        elif kind == "tx_sent":
            # Logged by the scheduler before the MAC confirms, so it attaches
            # to this node's next `val tx`.
            s.pending = (int(m.group(3)), m.group(2) == "true")
        elif kind == "tx_slot" and m.group(2) == "true":
            s.tx_shared_asns.add(int(m.group(1)))
        elif kind == "tx_noack":
            s.tx_noack_asns.append(int(m.group(1)))
        elif kind == "tx_drop":
            s.tx_drops.append(m.group(4))
            s.pending = (int(m.group(2)), None)
        elif kind == "tx":
            seq, ok = int(m.group(1)), m.group(2) == "1"
            attempts, shared = s.pending or (None, None)
            s.pending = None
            if ok:
                s.tx_ok += 1
                if attempts is not None:
                    s.tx_attempts.append(attempts)
                s.tx_shared += bool(shared)
            else:
                s.tx_fail += 1
            self.tx_rows.append((t_s, node, seq, int(ok), attempts, shared))
        elif kind == "rx":
            s.rx[m.group(2)].append(int(m.group(1)))
            self.rx_rows.append((t_s, node, int(m.group(1)), m.group(2)))
        elif kind == "sync":
            asn, src, err = int(m.group(1)), m.group(2), int(m.group(3))
            peer = m.group(4) or ""
            if src == "peer":
                s.sync_peer[peer].append((t_s, err))     # measured, not applied
            else:
                s.sync_times_s.append(t_s)
                s.joined_at_s = s.joined_at_s if s.joined_at_s is not None else t_s
                (s.sync_frame if src == "frame" else s.sync_ack).append((t_s, err))
            self.sync_rows.append((t_s, node, asn, src, err, peer))
        elif kind == "rx_drop":
            s.rx_drops[(m.group(2) or "unknown", m.group(3))] += 1

    # output

    def _write_csvs(self) -> None:
        for name, header, rows in (
            ("tx", ("time_s", "node", "seq", "ok", "attempts", "shared"), self.tx_rows),
            ("rx", ("time_s", "node", "seq", "src"), self.rx_rows),
            ("sync", ("time_s", "node", "asn", "src", "err_ns", "peer"), self.sync_rows),
        ):
            path = f"{self.variant}_{name}.csv"
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(sorted(rows))
            print(f"  {len(rows):6d} {name} records -> {path}")

    def _print_summary(self) -> None:
        print(f"\n  === {self.variant}: per-node summary ===")
        for node in sorted(self.nodes):
            self._print_node(node, self.nodes[node])
        print(f"\n  === {self.variant}: validation checks ===")
        for procedure, check, result in self._checks():
            print(f"  {procedure:<22} {check:<44} {result}")

    def _print_node(self, node: str, s: NodeState) -> None:
        out = lambda label, text: print(f"    {label:<9}: {text}")
        print(f"\n  {node} (role={s.role}, addr={s.address})")

        if s.join_ms is not None:
            out("join", f"{s.join_ms / 1000.0:.1f} s after boot, parent={s.join_parent}")
            out("entry", f"{s.scans} scan(s) over the run, "
                        f"{s.join_retries} returned no usable beacon")
        if s.assoc_short is not None:
            out("assoc", f"short=0x{s.assoc_short} at {s.assoc_ms / 1000.0:.1f} s")
        if s.assoc_peers:
            peers = ", ".join(f"{d[-4:]}->0x{sh}" for d, sh, _ in s.assoc_peers)
            out("children", f"{len(s.assoc_peers)} associated ({peers})")
        for peer, samples in sorted(s.neighbors.items()):
            rx, acked, attempts, etx, _ = samples[-1]
            lqis = [x[4] for x in samples]
            # lqi_ewma has no writer in the stack, so it reads 0 everywhere.
            quality = (f"lqi {statistics.fmean(lqis):.0f}" if any(lqis)
                       else "lqi n/a (not populated by the stack)")
            out(f"link {peer[-4:]}", f"rx {rx}, tx {acked}/{attempts} acked, "
                                     f"etx {etx / 128.0:.2f}, {quality}")
        if s.beacon_asns:
            out("beacons", f"{len(s.beacon_asns)}, period {_interval(s.beacon_asns):.2f} s")
        if s.keepalive_asns:
            out("keep-alv", f"{len(s.keepalive_asns)}, period "
                            f"{_interval(s.keepalive_asns):.2f} s, mean "
                            f"{statistics.fmean(s.keepalive_attempts):.2f} attempt(s)")
        if s.tx_ok or s.tx_fail:
            total = s.tx_ok + s.tx_fail
            retried = sum(a > 1 for a in s.tx_attempts)
            mean = statistics.fmean(s.tx_attempts) if s.tx_attempts else float("nan")
            out("data tx", f"{s.tx_ok}/{total} delivered "
                           f"({100.0 * s.tx_ok / total:.2f}%), {retried} needed a "
                           f"retransmission ({100.0 * retried / max(len(s.tx_attempts), 1):.1f}%), "
                           f"mean {mean:.2f} attempt(s), max {max(s.tx_attempts, default=0)}")
            if s.tx_drops:
                reasons = ", ".join(f"{r}={s.tx_drops.count(r)}"
                                    for r in sorted(set(s.tx_drops)))
                out("drops", f"{len(s.tx_drops)} ({reasons})")
        for source, seqs in s.rx.items():
            span, distinct = max(seqs) - min(seqs) + 1, len(set(seqs))
            dupes = len(seqs) - distinct
            out("data rx", f"{distinct}/{span} distinct from {source} "
                           f"(PDR {100.0 * distinct / span:.2f}%)"
                           f"{f', {dupes} DUPLICATE(S)' if dupes else ''}")

        _sync_line("sync frm", s.sync_frame, s.joined_at_s,
                   f"largest gap {_gap(s.sync_times_s):.1f} s")
        _sync_line("sync ack", s.sync_ack, s.joined_at_s, "us-quantised by the IE")
        for peer, errs in sorted(s.sync_peer.items()):
            _sync_line(f"peer {peer[-4:]}", errs, errs[0][0] if errs else None,
                       "measured here, applied nowhere")
        if s.rx_drops:
            out("rx drops", ", ".join(f"{k}/{c}={n}"
                                      for (k, c), n in sorted(s.rx_drops.items())))

    def _checks(self) -> list[tuple[str, str, str]]:
        """The rows of the paper's validation table, filled from the run."""
        rows: list[tuple[str, str, str]] = []
        add = lambda *row: rows.append(row)
        joining = {n: self.nodes[n] for n in sorted(self.joiners)}
        senders = {n: self.nodes[n] for n in sorted(self.senders)}
        retried = lambda st: sum(a > 1 for a in st.tx_attempts)

        booted = [n for n in sorted(self.expected) if self.nodes[n].role]
        add("Boot", "every configured node reported in",
            _verdict(len(booted), len(self.expected),
                     ", ".join(sorted(set(self.expected) - set(booted)))))

        joined = [n for n, s in joining.items() if s.join_ms is not None]
        add("Passive scan", "beacon found on the scan channel",
            _verdict(len(joined), len(joining), ", ".join(sorted(joined))))

        # An empty scan means the receive window ended before a beacon arrived.
        # The driver also raises that on a frame failing the address filter, so
        # on an occupied channel network entry becomes a lottery.
        empty = {n: s.join_retries for n, s in joining.items() if s.join_retries}
        add("Scan efficiency", "one scan per join, no empty scans",
            "pass" if not empty else "WARN (" + ", ".join(
                f"{n}: {joining[n].scans} scans, {c} empty"
                for n, c in sorted(empty.items())) + ")")

        add("Schedule learning", "advertised slotframe and links adopted",
            _verdict(len(joined), len(joining),
                     "traffic flows on learned cells" if joined else ""))

        assoc = [n for n, s in joining.items() if s.assoc_short is not None]
        add("Association", "short address assigned and configured",
            _verdict(len(assoc), len(joining),
                     ", ".join(f"0x{joining[n].assoc_short}" for n in sorted(assoc))))

        # Three beacon periods without a correction means the time source is lost.
        gaps = [(n, _gap(s.sync_times_s)) for n, s in joining.items() if s.sync_times_s]
        held = [n for n, g in gaps if g < 3 * BEACON_PERIOD_S]
        add("Synchronization", "corrected continuously, never lost",
            _verdict(len(held), len(joining),
                     f"largest gap {max((g for _, g in gaps), default=float('nan')):.1f} s"))

        beaconing = {n: s for n, s in self.nodes.items() if s.beacon_asns}
        add("Beaconing", "beacons at the configured period",
            "pass (" + ", ".join(f"{n} {_interval(s.beacon_asns):.2f} s"
                                 for n, s in sorted(beaconing.items())) + ")"
            if beaconing else "no data")

        keeping = {n: s for n, s in self.nodes.items() if s.keepalive_asns}
        add("Keep-alive",
            "sent between beacons; each ACK resyncs" if keeping else "sent between beacons",
            "pass (" + ", ".join(f"{n} {len(s.keepalive_asns)} @ "
                                 f"{_interval(s.keepalive_asns):.2f} s"
                                 for n, s in sorted(keeping.items())) + ")"
            if keeping else "none (data traffic kept the time source fresh)")

        # Catches a node stuck in association: it joins, tracks beacons, stays
        # silent, and every other row forgives it.
        active = [n for n, s in senders.items() if s.tx_ok or s.tx_fail]
        silent = sorted(set(senders) - set(active))
        add("Data traffic", "every configured sender transmitted",
            _verdict(len(active), len(senders),
                     f"silent: {', '.join(silent)}" if silent else ""))

        if active:
            ok = sum(s.tx_ok for s in senders.values())
            total = sum(s.tx_ok + s.tx_fail for s in senders.values())
            add("Acknowledged data", "every frame acknowledged",
                f"{'pass' if ok == total and not silent else 'FAIL'} "
                f"({ok}/{total}, {100.0 * ok / total:.2f}%; " + ", ".join(
                    f"{n}:{s.tx_ok}/{s.tx_ok + s.tx_fail}"
                    for n, s in senders.items() if s.tx_ok or s.tx_fail) + ")")

            drops = sum(len(s.tx_drops) for s in senders.values())
            per_sender = ", ".join(
                f"{n}: {retried(s)}/{len(s.tx_attempts)} "
                f"({100.0 * retried(s) / max(len(s.tx_attempts), 1):.1f}%)"
                f"{f' +{len(s.tx_drops)} dropped' if s.tx_drops else ''}"
                for n, s in senders.items() if s.tx_attempts)
            if self.variant == "v2-dedicated":
                # No shared link here, so this is not a CSMA-CA result: it is
                # the control saying the link itself is clean. A handful of
                # isolated ACK losses over an hour is still a clean link.
                total_r = sum(retried(s) for s in senders.values())
                n_att = sum(len(s.tx_attempts) for s in senders.values())
                clean = drops == 0 and total_r <= 0.001 * max(n_att, 1)
                add("TSCH CSMA-CA", "control arm: dedicated cells, no contention",
                    f"{'pass' if clean else 'FAIL'} ({total_r}/{n_att} retransmitted, "
                    f"{drops} dropped)")
            else:
                add("TSCH CSMA-CA", "backoff on the shared link, delivery holds",
                    f"{'pass' if drops == 0 else 'partial'} ({per_sender})")

            # Two senders in one cell transmit at the same instant, so which
            # frame survives is decided by received power, not by the MAC. An
            # aggregate rate averages a one-sided outcome away.
            rates = {n: retried(s) / max(len(s.tx_attempts), 1)
                     for n, s in senders.items() if s.tx_attempts}
            if len(rates) >= 2 and max(rates.values()) > 0:
                lo, hi = min(rates.values()), max(rates.values())
                add("Contention fairness", "both senders share the retransmission cost",
                    f"{'pass' if lo > 0.25 * hi else 'WARN'} ("
                    + ", ".join(f"{n} {100 * r:.1f}%" for n, r in sorted(rates.items()))
                    + ("" if lo > 0.25 * hi else
                       "; one radio wins every collision -- swap the two boards "
                       "between the roles to tell capture from a role asymmetry") + ")")

        # Frames accepted, acknowledged, then handed to nobody. Beacons land
        # here harmlessly once the scan is over; a MAC command cannot be
        # recovered by its sender.
        lost = {n: sum(c for (k, _), c in s.rx_drops.items() if k not in ("beacon", "unknown"))
                for n, s in self.nodes.items()}
        lost = {n: c for n, c in lost.items() if c}
        untyped = sum(c for s in self.nodes.values()
                      for (k, _), c in s.rx_drops.items() if k == "unknown")
        add("Reception", "no acknowledged frame dropped internally",
            "FAIL (" + ", ".join(f"{n}: {c}" for n, c in sorted(lost.items())) + ")" if lost
            else f"indeterminate ({untyped} drops; rebuild for type=)" if untyped
            else "pass")

        # Whether the shared arm produced contention at all. Retransmission
        # counts do not reveal that; shared cell instances used by more than
        # one sender do.
        sharing = {n: s.tx_shared_asns for n, s in self.nodes.items()
                   if n in self.senders and s.tx_shared_asns}
        if len(sharing) >= 2:
            overlap = set.intersection(*sharing.values())
            rate = 100.0 * len(overlap) / max(min(len(v) for v in sharing.values()), 1)
            noacks = sum(len(self.nodes[n].tx_noack_asns) for n in sharing)
            collided = sum(1 for n in sharing for a in self.nodes[n].tx_noack_asns
                           if any(a in sharing[o] for o in sharing if o != n))
            add("Shared-cell contention", "senders actually meet in the same cell",
                f"{'pass' if rate >= 1.0 else 'WARN' if overlap else 'FAIL'} "
                f"({len(overlap)} shared cell instances used by >1 sender, {rate:.3f}% "
                f"of transmissions; {collided}/{noacks} no-acks were collisions)")
        return rows


def _sync_line(label: str, samples: list[tuple], start_s, note: str) -> None:
    """One line of sync statistics in microseconds, after the warm-up."""
    if not samples or start_s is None:
        return
    errs = [e / 1000.0 for t, e in samples if t >= start_s + SYNC_WARMUP_S]
    if not errs:
        return
    absolute = sorted(abs(e) for e in errs)
    print(f"    {label:<9}: n={len(errs)} mean={statistics.fmean(errs):+.3f} "
          f"sd={statistics.pstdev(errs):.3f} p99|.|={_pct(absolute, 0.99):.3f} "
          f"max|.|={absolute[-1]:.3f} us, {note}")


def _pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _interval(asns: list[int]) -> float:
    """Median distance between consecutive events, in seconds, from their ASNs."""
    if len(asns) < 2:
        return float("nan")
    return statistics.median(b - a for a, b in zip(asns, asns[1:])) * SLOT_S


def _gap(times_s: list[float]) -> float:
    """Longest interval without a clock correction, in seconds."""
    if len(times_s) < 2:
        return float("nan")
    return max(b - a for a, b in zip(times_s, times_s[1:]))


def _verdict(done: int, expected: int, detail: str = "") -> str:
    status = "pass" if done == expected and expected > 0 else "FAIL"
    return f"{status} ({done}/{expected}{', ' + detail if detail else ''})"


# === EXPERIMENT ============================================================

def build_experiment(variant: str, run_s: float, jitter_ms: int | None = None):
    specs = node_specs(variant, jitter_ms)
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

    exp = Experiment(topology=topology, name=variant.replace("-", "_"))
    for start_s, node in starts:
        exp.after(start_s).do(StartNodeAction(node=node))
    exp.after(run_s).do(StopExperimentAction())
    return exp, devices, specs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", choices=sorted(DEFAULT_RUN_S), default="v1")
    ap.add_argument("--run", type=float, default=None, help="run length (s)")
    ap.add_argument("--pcap", default=None, help="pcap path (default /tmp/<variant>.pcap)")
    ap.add_argument("--quiet", action="store_true", help="do not echo serial lines")
    ap.add_argument("--send-jitter", type=int, default=None, metavar="MS",
                    help="override the +/- send jitter of every sender, in ms")
    args = ap.parse_args()

    run_s = args.run if args.run is not None else DEFAULT_RUN_S[args.variant]
    setup_logging(logging.INFO)
    exp, devices, specs = build_experiment(args.variant, run_s, args.send_jitter)

    jitter = sorted({s["env"]["SEND_JITTER_MS"] for s in specs if "SEND_JITTER_MS" in s["env"]})
    print(f"  send jitter: +/-{', '.join(jitter)} ms")
    settle_s = max(s["start_s"] for s in specs) + 3 * BEACON_PERIOD_S
    if run_s < settle_s + 60:
        print(f"  WARNING: only {max(run_s - settle_s, 0):.0f}s of steady state; "
              f"this is a smoke test, not a validation run")
    if run_s < DEFAULT_RUN_S[args.variant]:
        print(f"  WARNING: {args.variant} is specified as "
              f"{DEFAULT_RUN_S[args.variant]:.0f}s; frame counts from a "
              f"{run_s:.0f}s run must not be reported as the full arm")

    env = Local(
        devices=devices,
        flash_concurrency=len(devices),
        sniffers=Nrf802154Sniffer(channel=CHANNEL,
                                  pcap_path=args.pcap or f"/tmp/{args.variant}.pcap"),
    )
    exp.run(env, event_listener=ValidationListener(args.variant, specs,
                                                   echo=not args.quiet))


if __name__ == "__main__":
    main()
