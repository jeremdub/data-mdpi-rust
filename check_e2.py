#!/usr/bin/env python3
"""E2: Table 6, and the dot15d4 rows of Table 7 (E3a).

    python3 check_e2.py e2-sync_error/run1 e2-sync_error/run2 e2-sync_error/run3
    python3 check_e2.py e3a-dot15d4_47slots/run1 e3a-dot15d4_47slots/run2 ...

A run folder holds the Logic 2 export (Time [s], Channel 5 = the root's frame
pin, Channel 6 = leaf 10's receive-ready pin) and the node log (time_s, node,
asn, src, err_ns). The folders given are pooled. E3a is the same star on a
47-slot slotframe, so it reads the same way.
"""

import bisect
import csv
import glob
import math
import statistics
import sys

K = 1269625.0    # ns between the two rising edges: 1100 + 160 + 9.625 us
SLOT = 10e-3     # timeslot, s
WARMUP = 600.0   # s discarded at the start of a run
STEP = 125.0     # reported quantum, ns
HALF = 1800.0    # half-width of the sliding rate window, s
LEAF_PPM = 4.4   # the analyzer's clock against leaf 10's, from its slot grid in E1
LEAF = "child10"


def read_logic(path, names):
    """Times, and the level of each named channel at each of them."""
    handle = open(path)
    rows = csv.reader(handle)
    header = next(rows)
    columns = []
    for name in names:
        columns.append(header.index(name))
    times = []
    series = []
    for name in names:
        series.append([])
    for row in rows:
        times.append(float(row[0]))
        for i in range(len(columns)):
            series[i].append(int(row[columns[i]]))
    handle.close()
    return times, series


def edges(times, levels, going_up):
    """Every time one channel changes, in one direction."""
    out = []
    for i in range(1, len(times)):
        before = levels[i - 1]
        after = levels[i]
        if going_up and before == 0 and after == 1:
            out.append(times[i])
        elif not going_up and before == 1 and after == 0:
            out.append(times[i])
    return out


def beacon_times(times, root_frame):
    """The root's beacons. Its pin also carries other frames, so use the width."""
    rising = edges(times, root_frame, True)
    falling = edges(times, root_frame, False)
    out = []
    for start in rising:
        i = bisect.bisect_right(falling, start)
        if i >= len(falling):
            i = len(falling) - 1
        width_us = (falling[i] - start) * 1e6
        if 2500 < width_us < 2800:
            out.append(start)
    return out


def leaf_window(leaf_rising, beacon):
    """The leaf's receive-ready edge for one beacon, about 1.27 ms before it.

    None when there is not exactly one, which is a beacon the leaf missed.
    """
    first = bisect.bisect_left(leaf_rising, beacon - 1.35e-3)
    last = bisect.bisect_left(leaf_rising, beacon - 1.20e-3)
    if last - first == 1:
        return leaf_rising[first]
    return None


def read_sync(path):
    rows = []
    handle = open(path, newline="")
    for row in csv.DictReader(handle):
        if row["src"] != "frame":
            continue
        rows.append({"time_s": float(row["time_s"]),
                     "node": row["node"],
                     "asn": int(row["asn"]),
                     "err_ns": float(row["err_ns"])})
    handle.close()
    return rows


def align(beacons, asns):
    """How many beacons the analyzer saw before the first one the node logged.

    The beacon spacing alternates between two values, so matching the two
    sequences of gaps pins the offset down.
    """
    analyzer_gaps = []
    for a, b in zip(beacons, beacons[1:]):
        analyzer_gaps.append(round((b - a) / SLOT))
    node_gaps = []
    for a, b in zip(asns, asns[1:]):
        node_gaps.append(b - a)

    best_offset = 0
    best_score = -1
    for offset in range(12):
        score = 0
        for i in range(len(node_gaps)):
            if offset + i >= len(analyzer_gaps):
                break
            if analyzer_gaps[offset + i] == node_gaps[i]:
                score += 1
        if score > best_score:
            best_score = score
            best_offset = offset
    return best_offset


def slope(xs, ys):
    """Least-squares slope of ys against xs."""
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    top = 0.0
    bottom = 0.0
    for x, y in zip(xs, ys):
        top += (x - mean_x) * (y - mean_y)
        bottom += (x - mean_x) * (x - mean_x)
    return top / bottom


def rates_ppm(beacons, asns):
    """How fast the analyzer's clock runs against the root's, at each beacon.

    A one-hour window around the beacon, widened to 60 beacons at the ends.
    """
    out = []
    for centre in beacons:
        first = bisect.bisect_left(beacons, centre - HALF)
        last = bisect.bisect_left(beacons, centre + HALF)
        if first > len(beacons) - 60:
            first = len(beacons) - 60
        if last < 60:
            last = 60
        seconds_per_slot = slope(asns[first:last], beacons[first:last])
        out.append((seconds_per_slot / SLOT - 1) * 1e6)
    return out


def read_run(folder):
    times, series = read_logic(folder + "/logic-analyzer.csv",
                               ["Channel 5", "Channel 6"])
    root_frame = series[0]
    leaf_ready = series[1]
    beacons = beacon_times(times, root_frame)
    leaf_rising = edges(times, leaf_ready, True)
    rows = read_sync(glob.glob(folder + "/*sync*.csv")[0])

    asns = sorted(set(row["asn"] for row in rows))
    offset = align(beacons, asns)
    beacons = beacons[offset:offset + len(asns)]
    asns = asns[:len(beacons)]
    ppm = rates_ppm(beacons, asns)

    logged = {}
    for row in rows:
        if row["node"] == LEAF:
            logged[row["asn"]] = row["err_ns"]

    # The run started one slot grid before the first beacon we kept.
    start = beacons[0] - asns[0] * SLOT + WARMUP

    software = []
    analyzer = []
    corrected = []
    root = []
    for i in range(len(beacons)):
        beacon = beacons[i]
        if beacon <= start:
            continue
        window = leaf_window(leaf_rising, beacon)
        if window is None:
            continue
        if asns[i] not in logged:
            continue
        error = (window - beacon) * 1e9 + K
        software.append(logged[asns[i]])
        analyzer.append(error)
        # The leaf's clock times 1260 us of the K ns, so K takes its rate
        # (+5.6 ns). The root's rate, measured here, is kept for comparison.
        corrected.append(error + K * LEAF_PPM * 1e-6)
        root.append(error + K * ppm[i] * 1e-6)
    return software, analyzer, corrected, root, rows


def show(name, values):
    absolute = []
    for v in values:
        absolute.append(abs(v))
    print("  %-32s %6d %+7.1f %+7.1f %6.1f %6.1f %7.1f"
          % (name, len(values), statistics.fmean(values), statistics.median(values),
             statistics.stdev(values), statistics.fmean(absolute), max(absolute)))


folders = sys.argv[1:]
if not folders:
    folders = ["."]

software = []
analyzer = []
corrected = []
root = []
runs = []
for folder in folders:
    a, b, c, d, rows = read_run(folder)
    software += a
    analyzer += b
    corrected += c
    root += d
    runs.append((folder, rows))

difference = []
for s, c in zip(software, corrected):
    difference.append(s - c)

print("%s: %d paired beacons" % (", ".join(folders), len(software)))
print("  %-32s %6s %7s %7s %6s %6s %7s"
      % ("series (ns)", "n", "mean", "median", "sd", "MAE", "max"))
show("software estimate", software)
show("analyzer estimate", analyzer)
show("analyzer, leaf's rate", corrected)
show("difference, software - analyzer", difference)
print("  mean difference with the root's rate instead: %+.1f ns"
      % statistics.fmean([s - r for s, r in zip(software, root)]))

# The 125 ns grid. Its phase is the mean angle of the reported values taken
# around a circle of one step, which is what a plain mean cannot give.
sines = []
cosines = []
for value in software:
    angle = 2 * math.pi * value / STEP
    sines.append(math.sin(angle))
    cosines.append(math.cos(angle))
phase = math.atan2(statistics.fmean(sines), statistics.fmean(cosines))
phase = phase / (2 * math.pi) * STEP

per_group = {}
for value in software:
    group = round((value - phase) / STEP)
    per_group[group] = per_group.get(group, 0) + 1
counts = []
for group in sorted(per_group):
    counts.append(per_group[group])

# Does rounding the analyzer value to the grid give back what the node logged?
shift = statistics.fmean([s - a for s, a in zip(software, analyzer)])
same = 0
for s, a in zip(software, analyzer):
    if round((a + shift - phase) / STEP) == round((s - phase) / STEP):
        same += 1
print("  grid phase %+.1f ns, samples per 125 ns group %s, rounding reproduces %.1f%%"
      % (phase, counts, 100.0 * same / len(software)))

# Every leaf of every run, from the node log alone.
series = {}
total = 0
for folder, rows in runs:
    for row in rows:
        if row["time_s"] <= WARMUP:
            continue
        series.setdefault((folder, row["node"]), []).append(row["err_ns"])
        total += 1
sds = []
maes = []
for values in series.values():
    sds.append(statistics.stdev(values))
    maes.append(statistics.fmean([abs(v) for v in values]))
print("  %d samples over %d leaf-run series: sd %.1f-%.1f ns, MAE %.1f-%.1f ns"
      % (total, len(series), min(sds), max(sds), min(maes), max(maes)))
