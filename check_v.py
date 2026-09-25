#!/usr/bin/env python3
"""V1, V2a, V2b: Table 4, and the per-node and per-run supplementary tables.

    python3 check_v.py                 # the three runs of each, as laid out here
    python3 check_v.py V1=a/log.txt,b/log.txt V2a=... V2b=...

"""

import os
import re
import statistics
import sys

LINE = re.compile(r"^\s*\[\s*([0-9.]+)s\]\s+(\S+)\s+\|\s+\w+\s+(.*)$")
PATTERNS = [
    ("boot", re.compile(r"val boot node=(\d+) role=(\w+) addr=([0-9a-f]{16})")),
    ("scan", re.compile(r"val scan done found=(\d+) ms=(\d+)")),
    ("join", re.compile(r"val join ok parent=([0-9a-f]{16}) pan=([0-9a-f]+) ms=(\d+)")),
    ("join_retry", re.compile(r"val join retry ms=(\d+)")),
    ("assoc", re.compile(r"val assoc ok short=([0-9a-f]+) ms=(\d+)")),
    ("desync", re.compile(r"val desync")),
    ("tx", re.compile(r"val tx seq=(\d+) ok=(\d) ms=(\d+)")),
    ("rx", re.compile(r"val rx seq=(\d+) src=([0-9a-f]{16}) ms=(\d+)")),
    ("beacon", re.compile(r"tsch beacon sent asn=(\d+)")),
    ("keepalive", re.compile(r"tsch keep-alive sent asn=(\d+) attempts=(\d+)")),
    ("tx_sent", re.compile(r"tsch tx sent asn=(\d+) shared=(\w+) attempts=(\d+)")),
    ("tx_drop", re.compile(r"tsch tx drop asn=(\d+) nb=(\d+) max=(\d+) result=([\w-]+)")),
]
SLOT_S = 0.010
JOINING = [("n1", "N2"), ("n2", "N3")]


def new_node():
    return {"role": None, "addr": None, "scans": [], "join_ms": None, "parent": None,
            "join_retries": 0, "assoc_ms": None, "desync": 0, "tx": [], "rx": {},
            "beacons": [], "keepalives": [], "pending": None}


def parse(path):
    """Return {node: fields} for every node that printed a boot line."""
    nodes = {}
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = LINE.match(raw)
        if line is None or line.group(2) == "sniffer":
            continue
        name = line.group(2)
        text = line.group(3)
        if name not in nodes:
            nodes[name] = new_node()
        s = nodes[name]

        for kind, pattern in PATTERNS:
            m = pattern.search(text)
            if m is None:
                continue
            if kind == "boot":
                s["role"] = m.group(2)
                s["addr"] = m.group(3)
            elif kind == "scan":
                s["scans"].append(int(m.group(1)))
            elif kind == "join" and s["join_ms"] is None:
                s["parent"] = m.group(1)
                s["join_ms"] = int(m.group(3))
            elif kind == "join_retry":
                s["join_retries"] += 1
            elif kind == "assoc":
                s["assoc_ms"] = int(m.group(2))
            elif kind == "desync":
                s["desync"] += 1
            elif kind == "beacon":
                s["beacons"].append(int(m.group(1)))
            elif kind == "keepalive":
                s["keepalives"].append(int(m.group(1)))
            elif kind == "tx_sent":
                s["pending"] = int(m.group(3))      # attempts of the frame just sent
            elif kind == "tx_drop":
                s["pending"] = int(m.group(2))      # NB when the MAC gave up
            elif kind == "tx":
                delivered = m.group(2) == "1"
                s["tx"].append((delivered, int(m.group(3)), s["pending"]))
                s["pending"] = None
            elif kind == "rx":
                source = m.group(2)
                s["rx"].setdefault(source, []).append(int(m.group(1)))
            break

    out = {}
    for name in nodes:
        if nodes[name]["role"] is not None:
            out[name] = nodes[name]
    return out


def median_step(asns):
    if len(asns) < 2:
        return None
    steps = []
    for a, b in zip(asns, asns[1:]):
        steps.append(b - a)
    return statistics.median(steps)


def node_metrics(nodes, name):
    s = nodes[name]

    labels = {}
    for key in nodes:
        if nodes[key]["role"] == "root":
            labels[nodes[key]["addr"]] = "N1"
        else:
            labels[nodes[key]["addr"]] = dict(JOINING).get(key, key)

    delivered = []
    for ok, ms, attempts in s["tx"]:
        if ok:
            delivered.append(attempts)

    beacon_steps = []
    for a, b in zip(s["beacons"], s["beacons"][1:]):
        beacon_steps.append(b - a)

    uptime = []
    for ok, ms, attempts in s["tx"]:
        uptime.append(ms)

    # Duplicates of this node's frames, as seen by whoever received them.
    duplicates = 0
    for other in nodes.values():
        for source, seqs in other["rx"].items():
            if source == s["addr"]:
                duplicates += len(seqs) - len(set(seqs))

    attempt_counts = []
    for k in (1, 2, 3, 4):
        attempt_counts.append(delivered.count(k))

    span = 0
    if len(uptime) > 1:
        span = uptime[-1] - uptime[0]

    retried = len(s["tx"]) - len(delivered)     # dropped frames count as retried
    for attempts in delivered:
        if attempts > 1:
            retried += 1

    return {
        "scans": len(s["scans"]), "found": sorted(set(s["scans"])),
        "join_s": s["join_ms"] / 1000.0, "assoc_ms": s["assoc_ms"] - s["join_ms"],
        "parent": labels.get(s["parent"], s["parent"]),
        "join_retries": s["join_retries"], "desync": s["desync"],
        "beacons": len(s["beacons"]), "beacon_step": median_step(s["beacons"]),
        "beacon_min": min(beacon_steps, default=None),
        "beacon_max": max(beacon_steps, default=None),
        "keepalives": len(s["keepalives"]),
        "keepalive_step": median_step(s["keepalives"]),
        "requested": len(s["tx"]), "delivered": len(delivered),
        "dropped": len(s["tx"]) - len(delivered), "retry": retried,
        "attempt": attempt_counts, "attempt_sum": sum(delivered),
        "max_attempts": max(delivered, default=0), "span_ms": span,
        "duplicates_at_receiver": duplicates,
    }


def rng(values, digits):
    low = "%.*f" % (digits, min(values))
    high = "%.*f" % (digits, max(values))
    if low == high:
        return low
    return "%s-%s" % (low, high)


def total(runs, key):
    out = 0
    for run in runs:
        out += run[key]
    return out


def report(experiments):
    runs = {}
    for name, paths in experiments.items():
        runs[name] = []
        for path in paths:
            runs[name].append(parse(path))

    print("=== per run (Table S7) ===")
    print("%-4s %3s %9s %9s %8s %7s %6s %4s %9s %11s %6s"
          % ("exp", "run", "requested", "delivered", "ratio%", "dropped", "N2",
             "N3", "N2share%", "join(s)", "assoc"))

    per = {}
    beacons_by_root = {}
    for name, logs in runs.items():
        for i, nodes in enumerate(logs, 1):
            m = {}
            for node, label in JOINING:
                m[label] = node_metrics(nodes, node)
            per[(name, i)] = m

            requested = m["N2"]["requested"] + m["N3"]["requested"]
            delivered = m["N2"]["delivered"] + m["N3"]["delivered"]
            joins = [m["N2"]["join_s"], m["N3"]["join_s"]]
            assocs = [m["N2"]["assoc_ms"], m["N3"]["assoc_ms"]]
            print("%-4s %3d %9d %9d %8.2f %7d %6d %4d %9.2f %11s %6s"
                  % (name, i, requested, delivered, 100.0 * delivered / requested,
                     requested - delivered, m["N2"]["retry"], m["N3"]["retry"],
                     100.0 * m["N2"]["retry"] / m["N2"]["requested"],
                     rng(joins, 1), rng(assocs, 0)))

        beacons = 0
        for nodes in logs:
            for node in nodes.values():
                if node["role"] == "root":
                    beacons += len(node["beacons"])
        beacons_by_root[name] = beacons

    print("\n=== merged per node (Table S6) ===")
    for name, logs in runs.items():
        print("\n%s (%d runs), N1 beacons %d" % (name, len(logs), beacons_by_root[name]))
        for label in ("N2", "N3"):
            xs = []
            for i in range(1, len(logs) + 1):
                xs.append(per[(name, i)][label])

            attempts = []
            for k in range(4):
                count = 0
                for x in xs:
                    count += x["attempt"][k]
                attempts.append(count)

            if name == "V1":
                offered = 30000.0
            else:
                offered = 250.0
            intervals = total(xs, "requested") - len(xs)
            achieved = total(xs, "span_ms") / float(intervals)

            steps = []
            keepalive_steps = []
            for x in xs:
                if x["beacon_step"]:
                    steps.append(x["beacon_step"] * SLOT_S)
                if x["keepalive_step"]:
                    keepalive_steps.append(x["keepalive_step"] * SLOT_S)

            scans = set()
            joins = []
            assocs = []
            for x in xs:
                scans.add(x["scans"])
                joins.append(x["join_s"])
                assocs.append(x["assoc_ms"])

            print("  %s parent %s, scans/join %s, found %s, join %s s, assoc %s ms, "
                  "join retries %d, desync %d"
                  % (label, xs[0]["parent"], sorted(scans), xs[0]["found"],
                     rng(joins, 1), rng(assocs, 0), total(xs, "join_retries"),
                     total(xs, "desync")))
            if total(xs, "beacons"):
                lowest = min(x["beacon_min"] for x in xs)
                highest = max(x["beacon_max"] for x in xs)
                print("     beacons %d, median period %s s, interval %d-%d slots"
                      % (total(xs, "beacons"), rng(steps, 2), lowest, highest))
            if total(xs, "keepalives"):
                print("     keep-alives %d, median interval %s s"
                      % (total(xs, "keepalives"), rng(keepalive_steps, 2)))
            print("     requested %d, delivered %d (%.2f%%), dropped %d, retry %d "
                  "(%.2f%%), mean attempts %.3f, max %d"
                  % (total(xs, "requested"), total(xs, "delivered"),
                     100.0 * total(xs, "delivered") / total(xs, "requested"),
                     total(xs, "dropped"), total(xs, "retry"),
                     100.0 * total(xs, "retry") / total(xs, "requested"),
                     total(xs, "attempt_sum") / float(total(xs, "delivered")),
                     max(x["max_attempts"] for x in xs)))
            print("     achieved interval %.1f ms (%.1f%% of offered), attempts 1-4 "
                  "%s, failed %d, duplicates at receiver %d"
                  % (achieved, 100.0 * offered / achieved, attempts,
                     total(xs, "dropped"), total(xs, "duplicates_at_receiver")))

    print("\n=== merged per experiment (Table 4) ===")
    for name, logs in runs.items():
        xs = []
        for i in range(1, len(logs) + 1):
            for label in ("N2", "N3"):
                xs.append(per[(name, i)][label])

        joins = []
        assocs = []
        first_attempt = 0
        for x in xs:
            joins.append(x["join_s"])
            assocs.append(x["assoc_ms"])
            first_attempt += x["attempt"][0]

        print("%-4s join %s s, assoc %s ms, desync %d, requested %d, delivered %d "
              "(%.2f%%), first attempt %d, retry %d, dropped %d, max attempts %d"
              % (name, rng(joins, 1), rng(assocs, 0), total(xs, "desync"),
                 total(xs, "requested"), total(xs, "delivered"),
                 100.0 * total(xs, "delivered") / total(xs, "requested"),
                 first_attempt, total(xs, "retry"), total(xs, "dropped"),
                 max(x["max_attempts"] for x in xs)))


def main():
    if len(sys.argv) > 1:
        experiments = {}
        for arg in sys.argv[1:]:
            name, paths = arg.split("=", 1)
            experiments[name] = paths.split(",")
    else:
        layout = {"V1": "v1-validation_procedures/run%d/log.txt",
                  "V2a": "v2-validation_shared_dedicated_cells/variant-dedicated/run%d/log.txt",
                  "V2b": "v2-validation_shared_dedicated_cells/variant-shared/run%d/log.txt"}
        experiments = {}
        for name, pattern in layout.items():
            experiments[name] = [pattern % i for i in (1, 2, 3)]

    missing = []
    for paths in experiments.values():
        for path in paths:
            if not os.path.exists(path):
                missing.append(path)
    if missing:
        print("missing logs: %s\n%s" % (", ".join(missing), __doc__))
        return 2

    report(experiments)
    return 0


if __name__ == "__main__":
    sys.exit(main())
