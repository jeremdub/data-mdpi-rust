#!/usr/bin/env python3
"""V1, V2a and V2b: Table 5 and Supplementary Tables S6 and S7.

    python3 check_v.py            # runs 1 to 10
    python3 check_v.py 1 2 3      # some runs
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
LAYOUT = {"V1": "v1-validation_procedures/run%d/log.txt",
          "V2a": "v2-validation_shared_dedicated_cells/variant-dedicated/run%d/log.txt",
          "V2b": "v2-validation_shared_dedicated_cells/variant-shared/run%d/log.txt"}
RAW = {"N2": "n1", "N3": "n2"}
OFFERED = {"V1": 30000.0, "V2a": 250.0, "V2b": 250.0}


def parse(path):
    nodes = {}
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = LINE.match(raw)
        if line is None or line.group(2) == "sniffer":
            continue
        t = float(line.group(1))
        s = nodes.setdefault(line.group(2), {
            "role": None, "addr": None, "scans": [], "join_ms": None, "parent": None,
            "join_retries": 0, "assoc_ms": None, "desync": 0, "tx": [], "rx": {},
            "beacons": [], "keepalives": [], "pending": None, "boot_t": None, "join_t": None})
        for kind, pattern in PATTERNS:
            m = pattern.search(line.group(3))
            if m is None:
                continue
            g = m.groups()
            if kind == "boot":
                s["role"], s["addr"] = g[1], g[2]
                if s["boot_t"] is None:
                    s["boot_t"] = t
            elif kind == "scan":
                s["scans"].append(int(g[0]))
            elif kind == "join" and s["join_ms"] is None:
                s["parent"], s["join_ms"], s["join_t"] = g[0], int(g[2]), t
            elif kind == "join_retry":
                s["join_retries"] += 1
            elif kind == "assoc":
                s["assoc_ms"] = int(g[1])
            elif kind == "desync":
                s["desync"] += 1
            elif kind == "beacon":
                s["beacons"].append(int(g[0]))
            elif kind == "keepalive":
                s["keepalives"].append(int(g[0]))
            elif kind == "tx_sent":
                s["pending"] = int(g[2])
            elif kind == "tx_drop":
                s["pending"] = int(g[1])
            elif kind == "tx":
                s["tx"].append((g[1] == "1", int(g[2]), s["pending"]))
                s["pending"] = None
            elif kind == "rx":
                s["rx"].setdefault(g[1], []).append(int(g[0]))
            break
    return {k: v for k, v in nodes.items() if v["role"] is not None}


def median_step(asns):
    return statistics.median(b - a for a, b in zip(asns, asns[1:])) if len(asns) > 1 else None


def node_metrics(nodes, name):
    s = nodes[name]
    alias = {raw: label for label, raw in RAW.items()}
    labels = {n["addr"]: "N1" if n["role"] == "root" else alias.get(k, k) for k, n in nodes.items()}
    delivered = [a for ok, _, a in s["tx"] if ok]
    steps = [b - a for a, b in zip(s["beacons"], s["beacons"][1:])]
    ms = [m for _, m, _ in s["tx"]]
    return {
        "scans": len(s["scans"]), "found": sorted(set(s["scans"])),
        "join_s": s["join_ms"] / 1000.0, "assoc_ms": s["assoc_ms"] - s["join_ms"],
        "parent": labels.get(s["parent"], s["parent"]), "join_retries": s["join_retries"],
        "desync": s["desync"], "boot_t": s["boot_t"], "join_t": s["join_t"],
        "beacons": len(s["beacons"]), "beacon_step": median_step(s["beacons"]),
        "beacon_min": min(steps, default=None), "beacon_max": max(steps, default=None),
        "keepalives": len(s["keepalives"]), "keepalive_step": median_step(s["keepalives"]),
        "requested": len(s["tx"]), "delivered": len(delivered),
        "dropped": len(s["tx"]) - len(delivered),
        "retry": len(s["tx"]) - len(delivered) + sum(1 for a in delivered if a > 1),
        "attempt": [delivered.count(k) for k in (1, 2, 3, 4)], "attempt_sum": sum(delivered),
        "max_attempts": max(delivered, default=0),
        "span_ms": ms[-1] - ms[0] if len(ms) > 1 else 0,
        "duplicates_at_receiver": sum(len(q) - len(set(q)) for o in nodes.values()
                                      for src, q in o["rx"].items() if src == s["addr"]),
    }


def load(root=".", runs=range(1, 11)):
    per, roots = {}, {}
    for name, pattern in LAYOUT.items():
        for i in runs:
            nodes = parse(os.path.join(root, pattern % i))
            per[(name, i)] = {label: node_metrics(nodes, raw) for label, raw in RAW.items()}
            roots.setdefault(name, []).extend(n["beacons"] for n in nodes.values() if n["role"] == "root")
    return per, roots


def rng(values, digits, sep="-"):
    low, high = "%.*f" % (digits, min(values)), "%.*f" % (digits, max(values))
    return low if low == high else low + sep + high


def tot(xs, key):
    return sum(x[key] for x in xs)


def report(per, roots, runs):
    print("=== per run (Table S7) ===")
    print("%-4s %3s %9s %9s %8s %7s %6s %4s %9s %11s %6s" % (
        "exp", "run", "requested", "delivered", "ratio%", "dropped", "N2", "N3", "N2share%",
        "join(s)", "assoc"))
    for name in LAYOUT:
        for i in runs:
            a, b = per[(name, i)]["N2"], per[(name, i)]["N3"]
            q, d = a["requested"] + b["requested"], a["delivered"] + b["delivered"]
            print("%-4s %3d %9d %9d %8.2f %7d %6d %4d %9.2f %11s %6s" % (
                name, i, q, d, 100.0 * d / q, q - d, a["retry"], b["retry"],
                100.0 * a["retry"] / a["requested"], rng([a["join_s"], b["join_s"]], 1),
                rng([a["assoc_ms"], b["assoc_ms"]], 0)))

    print("\n=== merged per node (Table S6) ===")
    for name in LAYOUT:
        print("\n%s (%d runs), N1 beacons %d" % (name, len(runs), sum(len(b) for b in roots[name])))
        for label in RAW:
            xs = [per[(name, i)][label] for i in runs]
            achieved = tot(xs, "span_ms") / float(tot(xs, "requested") - len(xs))
            print("  %s parent %s, scans/join %s, found %s, join %s s, assoc %s ms, "
                  "join retries %d, desync %d" % (
                      label, xs[0]["parent"], sorted({x["scans"] for x in xs}), xs[0]["found"],
                      rng([x["join_s"] for x in xs], 1), rng([x["assoc_ms"] for x in xs], 0),
                      tot(xs, "join_retries"), tot(xs, "desync")))
            if tot(xs, "beacons"):
                print("     beacons %d, median period %s s, interval %d-%d slots" % (
                    tot(xs, "beacons"), rng([x["beacon_step"] * SLOT_S for x in xs if x["beacon_step"]], 2),
                    min(x["beacon_min"] for x in xs), max(x["beacon_max"] for x in xs)))
            if tot(xs, "keepalives"):
                print("     keep-alives %d, median interval %s s" % (
                    tot(xs, "keepalives"),
                    rng([x["keepalive_step"] * SLOT_S for x in xs if x["keepalive_step"]], 2)))
            print("     requested %d, delivered %d (%.2f%%), dropped %d, retry %d (%.2f%%), "
                  "mean attempts %.3f, max %d" % (
                      tot(xs, "requested"), tot(xs, "delivered"),
                      100.0 * tot(xs, "delivered") / tot(xs, "requested"), tot(xs, "dropped"),
                      tot(xs, "retry"), 100.0 * tot(xs, "retry") / tot(xs, "requested"),
                      tot(xs, "attempt_sum") / float(tot(xs, "delivered")),
                      max(x["max_attempts"] for x in xs)))
            print("     achieved interval %.1f ms (%.1f%% of offered), attempts 1-4 %s, failed %d, "
                  "duplicates at receiver %d" % (
                      achieved, 100.0 * OFFERED[name] / achieved,
                      [sum(x["attempt"][k] for x in xs) for k in range(4)],
                      tot(xs, "dropped"), tot(xs, "duplicates_at_receiver")))

    print("\n=== merged per experiment (Table 5) ===")
    for name in LAYOUT:
        xs = [per[(name, i)][label] for i in runs for label in RAW]
        print("%-4s join %s s, assoc %s ms, desync %d, requested %d, delivered %d (%.2f%%), "
              "first attempt %d, retry %d, dropped %d, max attempts %d" % (
                  name, rng([x["join_s"] for x in xs], 1), rng([x["assoc_ms"] for x in xs], 0),
                  tot(xs, "desync"), tot(xs, "requested"), tot(xs, "delivered"),
                  100.0 * tot(xs, "delivered") / tot(xs, "requested"),
                  sum(x["attempt"][0] for x in xs), tot(xs, "retry"), tot(xs, "dropped"),
                  max(x["max_attempts"] for x in xs)))


if __name__ == "__main__":
    runs = [int(a) for a in sys.argv[1:]] or list(range(1, 11))
    report(*load(".", runs), runs)
