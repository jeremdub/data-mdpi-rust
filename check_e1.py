#!/usr/bin/env python3
"""E1: Table 6 and Supplementary Table S2, from the analyzer exports.

    python3 check_e1.py                               # e1-determinism/run1 to run10
    python3 check_e1.py e1-determinism/longer-run     # one run folder

A run folder holds coordinator-logic_analyzer.csv and leaf-logic_analyzer.csv.
Values come per run, then pooled over the runs, as the paper reports them.
"""
import csv
import glob
import hashlib
import os
import statistics
import sys

NAMES = "SCTREPF"
SHR, PHR, OCTET = 160.0, 32.0, 32.0
RAMP, TX_FS, RX_FS = 129.625, 9.625, 212.713
KA_PSDU, ACK_WAIT = 23, 400.0
WARMUP_S, GAP_US = 600.0, 8000.0
COORD_PAIR, LEAF_PAIR = 30000.0, 50000.0
ROWS = [("1", "Slot spacing, coord.", 0, "spacing", "30000"),
        ("1", "Slot spacing, leaf", 1, "spacing", "50000"),
        ("2", "Crystal lead, coord.", 0, "lead", "> 0"),
        ("2", "Crystal lead, leaf", 1, "lead", "> 0"),
        ("3", "Radio ramp, coord.", 0, "ramp", "129.625"),
        ("3", "Radio ramp, leaf", 1, "ramp", "129.625"),
        ("4", "Launch margin, coord.", 0, "margin", "16"),
        ("4", "Launch margin, leaf", 1, "margin", "16"),
        ("5", "RxAckDelay - SHR, leaf", 1, "listen", "640"),
        ("6", "Ack launch, coord.", 0, "launch", "--"),
        ("7", "TxAckDelay, leaf", 1, "ack", "1000"),
        ("7", "TxAckDelay, coord.", 0, "ack", "1000")]


def run_folders(args, default):
    out = []
    for a in args or [default]:
        runs = [d for d in glob.glob(os.path.join(a, "run*")) if os.path.isdir(d)]
        out += sorted(runs, key=lambda d: int(d.rsplit("run", 1)[1])) or [a]
    return out


def read_edges(path):
    edges, previous = [], None
    with open(path) as handle:
        rows = csv.reader(handle)
        next(rows)
        for row in rows:
            t = float(row[0]) * 1e6
            levels = [int(v) for v in row[1:8]]
            if previous is not None:
                for i in range(7):
                    if previous[i] != levels[i]:
                        edges.append((t, "f" if NAMES[i] == "F" and levels[i] == 0 else NAMES[i]))
            previous = levels
    return edges


def episodes(edges):
    out = [[edges[0]]]
    for edge in edges[1:]:
        if edge[0] - out[-1][-1][0] > GAP_US:
            out.append([edge])
        else:
            out[-1].append(edge)
    return out


def samples():
    s = {k: [] for k in ("spacing", "lead", "ramp", "margin", "listen", "launch", "ack", "window")}
    s.update(keep_alives=0, acked=0)
    return s


def measure(path, pair):
    s, enables = samples(), []
    for episode in episodes(read_edges(path)):
        if episode[0][0] < WARMUP_S * 1e6:
            continue
        sig = "".join(e[1] for e in episode)
        t = [e[0] for e in episode]
        if sig in ("SCER", "SCERFPf"):
            s["lead"].append(t[2] - t[1])
            s["ramp"].append(t[3] - t[2])
            enables.append(t[2])
        elif sig == "SCTEFfP":
            s["lead"].append(t[2] - RAMP - t[1])
            s["margin"].append(t[3] - t[2])
        elif sig in ("SCTEFfPERFPf", "SCTEFfPER"):
            s["lead"].append(t[2] - RAMP - t[1])
            s["margin"].append(t[3] - t[2])
            s["ramp"].append(t[8] - t[7])
            enables.append(t[7])
            s["keep_alives"] += 1
            listen = t[8] - t[6]
            s["listen"].append(listen)
            if sig == "SCTEFfPERFPf":
                rmarker = t[9] - RX_FS + SHR
                s["ack"].append(rmarker - t[6])
                s["window"].append(rmarker - (t[6] + listen + SHR))
                s["acked"] += 1
        elif sig == "SCERFPfETFfP":
            s["lead"].append(t[2] - t[1])
            s["ramp"].append(t[3] - t[2])
            enables.append(t[2])
            rmarker = t[9] - TX_FS
            s["launch"].append(rmarker - t[8])
            s["ack"].append(rmarker - (t[4] - RX_FS + SHR + PHR + KA_PSDU * OCTET))
    s["spacing"] = [b - a for a, b in zip(enables, enables[1:]) if abs(b - a - pair) < 5.0]
    return s


def report(title, coord, leaf):
    print("\n=== %s" % title)
    print("%3s  %-30s%8s%14s%10s%12s%12s"
          % ("", "Quantity (us)", "n", "Median", "sd (ns)", "Range (ns)", "Nominal"))
    for tag, label, board, key, nominal in ROWS:
        v = (coord, leaf)[board][key]
        if v:
            print("%3s  %-30s%8d%14.3f%10.1f%12.1f%12s"
                  % (tag, label, len(v), statistics.median(v), statistics.pstdev(v) * 1000.0,
                     (max(v) - min(v)) * 1000.0, nominal))
    print()
    for tag, key in (("2", "lead"), ("3", "ramp"), ("4", "margin")):
        print("  (%s) both boards pooled: n = %d" % (tag, len(coord[key] + leaf[key])))
    print("  crystal ready at least %.3f us before the ramp" % min(coord["lead"] + leaf["lead"]))
    w = leaf["window"]
    if w:
        after = statistics.median(w)
        print("  ack arrives %.3f us after the window opens and %.3f us before it closes"
              % (after, ACK_WAIT - after))
        print("  that is %.0f ns from the centre, and never within %.3f us of either edge"
              % ((ACK_WAIT / 2.0 - after) * 1000.0, min(min(x, ACK_WAIT - x) for x in w)))
    print("  keep-alives sent %d, acknowledged on the first attempt %d"
          % (leaf["keep_alives"], leaf["acked"]))


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


if __name__ == "__main__":
    folders = run_folders(sys.argv[1:], "e1-determinism")
    pooled = (samples(), samples())
    for folder in folders:
        coord_path = folder + "/coordinator-logic_analyzer.csv"
        leaf_path = folder + "/leaf-logic_analyzer.csv"
        use_leaf = digest(coord_path) != digest(leaf_path)
        if not use_leaf:
            print("%s: leaf export is a copy of the coordinator export, leaf half skipped" % folder)
        boards = (measure(coord_path, COORD_PAIR), measure(leaf_path, LEAF_PAIR) if use_leaf else samples())
        report(folder, *boards)
        for one, total in zip(boards, pooled):
            for key in one:
                total[key] += one[key]
    if len(folders) > 1:
        report("all runs pooled", *pooled)
