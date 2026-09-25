#!/usr/bin/env python3
"""E3b and E3c: the Contiki-NG rows of Table 7.

    python3 check_e3.py e3b-e3c-sync_error_contiki/e3b-run1 ... # 16 us tick
    python3 check_e3.py e3b-e3c-sync_error_contiki/e3c-run1 ... # 1 us tick

A run folder holds the Logic 2 export (Time [s], Channel 9 = the coordinator's
frame pin, Channel 10 = the leaf's slot pin, Channel 11 = the timer compare
that starts the leaf's slot) and the node log (time_s, node, asn, src, err_ns).
The folders given are pooled. The dot15d4 rows of the same table come from
check_e2.py.
"""

import bisect
import csv
import glob
import math
import statistics
import sys

SLOT = 0.010     # timeslot, s
WARMUP = 600.0   # s discarded at the start of a run
LOW = 1900.0     # the leaf starts its slot about 2.13 ms before the RMARKER,
HIGH = 2400.0    # so a lead outside this window is a slot the leaf missed


def rising_edges(path, name):
    """Every time the named channel goes from 0 to 1."""
    out = []
    handle = open(path)
    rows = csv.reader(handle)
    header = next(rows)
    column = header.index(name)
    previous = None
    for row in rows:
        level = int(row[column])
        if previous == 0 and level == 1:
            out.append(float(row[0]))
        previous = level
    handle.close()
    return out


def read_sync(path):
    rows = []
    handle = open(path, newline="")
    for row in csv.DictReader(handle):
        rows.append({"time_s": float(row["time_s"]),
                     "asn": int(row["asn"]),
                     "err_us": float(row["err_ns"]) / 1000.0})
    handle.close()
    return rows


def leads(beacons, compares):
    """How long before each beacon the leaf started its slot, in us.

    Beacons the leaf did not open a slot for are dropped.
    """
    kept_beacons = []
    kept_leads = []
    for beacon in beacons:
        i = bisect.bisect_left(compares, beacon) - 1
        if i < 0:
            i = 0
        if i >= len(compares):
            i = len(compares) - 1
        lead = (beacon - compares[i]) * 1e6
        if LOW < lead < HIGH:
            kept_beacons.append(beacon)
            kept_leads.append(lead)
    return kept_beacons, kept_leads


def align(beacons, asns):
    """Line the analyzer beacons up with the logged ones, by their gaps.

    Returns the offset into the analyzer series and how many pairs it gives.
    """
    analyzer_gaps = []
    for a, b in zip(beacons, beacons[1:]):
        analyzer_gaps.append(round((b - a) / SLOT))
    node_gaps = []
    for a, b in zip(asns, asns[1:]):
        node_gaps.append(b - a)

    best_offset = 0
    best_score = -1
    best_pairs = 0
    for offset in range(len(analyzer_gaps) - len(node_gaps) + 4):
        pairs = min(len(node_gaps), len(analyzer_gaps) - offset)
        score = 0
        for i in range(pairs):
            if analyzer_gaps[offset + i] == node_gaps[i]:
                score += 1
        if score > best_score:
            best_score = score
            best_offset = offset
            best_pairs = pairs
    return best_offset, best_pairs + 1, best_score


def read_run(folder):
    path = folder + "/logic-analyzer.csv"
    beacons = rising_edges(path, "Channel 9")     # RMARKER of each beacon on air
    compares = rising_edges(path, "Channel 11")   # the leaf's slot-start compares
    beacons, lead = leads(beacons, compares)

    rows = read_sync(glob.glob(folder + "/*interop*.csv")[0])
    asns = []
    for row in rows:
        asns.append(row["asn"])
    offset, n, score = align(beacons, asns)
    print("  %s: matched %d of %d gaps at offset %d" % (folder, score, n - 1, offset))
    lead = lead[offset:offset + n]
    rows = rows[:n]

    # The constant part of the lead is the timeslot template plus the probe
    # offsets, so only the variation around it is the error.
    middle = statistics.median(lead)

    software = []
    analyzer = []
    for row, value in zip(rows, lead):
        if row["time_s"] <= WARMUP:
            continue
        software.append(row["err_us"])
        analyzer.append(middle - value)
    return software, analyzer


def timestamp_step(values):
    """The stack's own tick, read off the values it logged."""
    step = 0
    for value in values:
        ticks = abs(round(value * 1000))
        if ticks > 0:
            step = math.gcd(step, ticks)
    return step / 1000.0


def show(name, values, step):
    absolute = []
    for v in values:
        absolute.append(abs(v))
    mae = statistics.fmean(absolute)
    print("  %-22s %6d %7.3f %7.3f %7.3f %7.2f"
          % (name, len(values), statistics.stdev(values), mae, max(absolute), mae / step))


folders = sys.argv[1:]
if not folders:
    folders = ["."]

software = []
analyzer = []
for folder in folders:
    a, b = read_run(folder)
    software += a
    analyzer += b

step = timestamp_step(software)
print("%s: %d paired beacons, step %.3f us" % (", ".join(folders), len(software), step))
print("  %-22s %6s %7s %7s %7s %7s" % ("series (us)", "n", "sd", "MAE", "max", "MAE/step"))
show("software estimate", software, step)
show("analyzer estimate", analyzer, step)

# How often the node logs a plain zero, and whether rounding the analyzer
# value to the node's own tick gives back what the node logged.
zeros = 0
same = 0
for s, a in zip(software, analyzer):
    if s == 0:
        zeros += 1
    if round(a / step) * step == s:
        same += 1
print("  reported as zero %.1f%%, rounding reproduces the log %.1f%%"
      % (100.0 * zeros / len(software), 100.0 * same / len(software)))
