#!/usr/bin/env python3
"""E2: Table 7 and Supplementary Table S8. E3a: the dot15d4 rows of Table 8 and Table S9.

    python3 check_e2.py                               # e2-sync_error/run1 to run10
    python3 check_e2.py e3a-dot15d4_47slots           # E3a, the same star on 47 slots
    python3 check_e2.py e2-sync_error/longer-run      # one run folder

A run folder holds the analyzer export (Channel 5: the root's frame pin,
Channel 6: leaf 10's receive-ready pin) and the node log. Values come per run,
then pooled, then as 95% confidence intervals over the runs (Student's t).
"""
import bisect
import csv
import glob
import math
import os
import statistics
import sys

K = 1269625.0
SLOT = 10e-3
WARMUP = 600.0
STEP = 125.0
HALF = 1800.0
LEAF_PPM = 4.4
LEAF = "child10"
T975 = {1: 12.7062, 2: 4.3027, 3: 3.1824, 4: 2.7764, 5: 2.5706, 6: 2.4469, 7: 2.3646,
        8: 2.3060, 9: 2.2622, 10: 2.2281, 11: 2.2010, 12: 2.1788, 13: 2.1604, 14: 2.1448}


def run_folders(args, default):
    out = []
    for a in args or [default]:
        runs = [d for d in glob.glob(os.path.join(a, "run*")) if os.path.isdir(d)]
        out += sorted(runs, key=lambda d: int(d.rsplit("run", 1)[1])) or [a]
    return out


def read_logic(path):
    with open(path) as handle:
        rows = csv.reader(handle)
        header = next(rows)
        a, b = header.index("Channel 5"), header.index("Channel 6")
        return [(float(r[0]), int(r[a]), int(r[b])) for r in rows]


def edges(data, col, up):
    step = (0, 1) if up else (1, 0)
    return [data[i][0] for i in range(1, len(data)) if (data[i - 1][col], data[i][col]) == step]


def beacon_times(data):
    rising, falling = edges(data, 1, True), edges(data, 1, False)
    out = []
    for start in rising:
        i = min(bisect.bisect_right(falling, start), len(falling) - 1)
        if 2500 < (falling[i] - start) * 1e6 < 2800:
            out.append(start)
    return out


def leaf_window(rising, beacon):
    first = bisect.bisect_left(rising, beacon - 1.35e-3)
    last = bisect.bisect_left(rising, beacon - 1.20e-3)
    return rising[first] if last - first == 1 else None


def read_sync(path):
    with open(path, newline="") as handle:
        return [{"time_s": float(r["time_s"]), "node": r["node"], "asn": int(r["asn"]),
                 "err_ns": float(r["err_ns"])} for r in csv.DictReader(handle) if r["src"] == "frame"]


def align(beacons, asns):
    a = [round((y - x) / SLOT) for x, y in zip(beacons, beacons[1:])]
    n = [y - x for x, y in zip(asns, asns[1:])]
    scores = [sum(1 for i in range(min(len(n), len(a) - o)) if a[o + i] == n[i]) for o in range(12)]
    return scores.index(max(scores))


def slope(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    return (sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            / sum((x - mx) * (x - mx) for x in xs))


def rates_ppm(beacons, asns):
    out = []
    for centre in beacons:
        first = min(bisect.bisect_left(beacons, centre - HALF), len(beacons) - 60)
        last = max(bisect.bisect_left(beacons, centre + HALF), 60)
        out.append((slope(asns[first:last], beacons[first:last]) / SLOT - 1) * 1e6)
    return out


def read_run(folder):
    data = read_logic(folder + "/logic-analyzer.csv")
    beacons, rising = beacon_times(data), edges(data, 2, True)
    rows = read_sync(glob.glob(folder + "/*sync*.csv")[0])
    asns = sorted({r["asn"] for r in rows})
    offset = align(beacons, asns)
    beacons = beacons[offset:offset + len(asns)]
    asns = asns[:len(beacons)]
    ppm = rates_ppm(beacons, asns)
    logged = {r["asn"]: r["err_ns"] for r in rows if r["node"] == LEAF}
    start = beacons[0] - asns[0] * SLOT + WARMUP
    sw, an, corrected, root = [], [], [], []
    for i, beacon in enumerate(beacons):
        window = leaf_window(rising, beacon)
        if beacon <= start or window is None or asns[i] not in logged:
            continue
        error = (window - beacon) * 1e9 + K
        sw.append(logged[asns[i]])
        an.append(error)
        corrected.append(error + K * LEAF_PPM * 1e-6)
        root.append(error + K * ppm[i] * 1e-6)
    return sw, an, corrected, root, rows


def mean_abs(values):
    return statistics.fmean(abs(v) for v in values)


def show(name, v):
    print("  %-32s %6d %+7.1f %+7.1f %6.1f %6.1f %7.1f"
          % (name, len(v), statistics.fmean(v), statistics.median(v), statistics.stdev(v),
             mean_abs(v), max(abs(x) for x in v)))


def interval(name, values):
    centre, spread = statistics.fmean(values), statistics.stdev(values)
    half = T975[len(values) - 1] * spread / math.sqrt(len(values))
    print("  %-32s %7.2f %7.2f   %6.2f to %6.2f" % (name, centre, spread, centre - half, centre + half))


if __name__ == "__main__":
    folders = run_folders(sys.argv[1:], "e2-sync_error")
    software, analyzer, corrected, root, logs, per_run = [], [], [], [], [], []
    for folder in folders:
        a, b, c, d, rows = read_run(folder)
        software += a
        analyzer += b
        corrected += c
        root += d
        logs.append((folder, rows))
        per_run.append((folder, a, c))

    if len(per_run) > 1:
        print("Each run: the probed leaf, in nanoseconds")
        print("  %-22s %6s %7s %7s %7s %7s %7s %7s"
              % ("run", "n", "sw sd", "sw MAE", "an sd", "an MAE", "d mean", "d sd"))
        for name, sw, an in per_run:
            d = [s - c for s, c in zip(sw, an)]
            print("  %-22s %6d %7.1f %7.1f %7.1f %7.1f %+7.1f %7.1f"
                  % (name, len(sw), statistics.stdev(sw), mean_abs(sw), statistics.stdev(an),
                     mean_abs(an), statistics.fmean(d), statistics.stdev(d)))
        print()

    print("%s: %d paired beacons" % (", ".join(folders), len(software)))
    print("  %-32s %6s %7s %7s %6s %6s %7s" % ("series (ns)", "n", "mean", "median", "sd", "MAE", "max"))
    show("software estimate", software)
    show("analyzer estimate", analyzer)
    show("analyzer, leaf's rate", corrected)
    show("difference, software - analyzer", [s - c for s, c in zip(software, corrected)])
    print("  mean difference with the root's rate instead: %+.1f ns"
          % statistics.fmean([s - r for s, r in zip(software, root)]))

    angles = [2 * math.pi * v / STEP for v in software]
    phase = math.atan2(statistics.fmean([math.sin(a) for a in angles]),
                       statistics.fmean([math.cos(a) for a in angles])) / (2 * math.pi) * STEP
    groups = {}
    for v in software:
        g = round((v - phase) / STEP)
        groups[g] = groups.get(g, 0) + 1
    shift = statistics.fmean([s - a for s, a in zip(software, analyzer)])
    same = sum(1 for s, a in zip(software, analyzer)
               if round((a + shift - phase) / STEP) == round((s - phase) / STEP))
    print("  grid phase %+.1f ns, samples per 125 ns group %s, rounding reproduces %.1f%%"
          % (phase, [groups[g] for g in sorted(groups)], 100.0 * same / len(software)))

    series = {}
    for folder, rows in logs:
        for r in rows:
            if r["time_s"] > WARMUP:
                series.setdefault((folder, r["node"]), []).append(r["err_ns"])
    sds = [statistics.stdev(v) for v in series.values()]
    maes = [mean_abs(v) for v in series.values()]
    print("  %d samples over %d leaf-run series: sd %.1f-%.1f ns, MAE %.1f-%.1f ns"
          % (sum(map(len, series.values())), len(series), min(sds), max(sds), min(maes), max(maes)))

    if len(per_run) > 1:
        print()
        print("95%% confidence interval over the %d runs (Student's t, %d df)"
              % (len(per_run), len(per_run) - 1))
        print("  %-32s %7s %7s   %s" % ("quantity (ns)", "mean", "sd", "95% CI"))
        diffs = [[s - c for s, c in zip(sw, an)] for _, sw, an in per_run]
        interval("software MAE", [mean_abs(sw) for _, sw, _ in per_run])
        interval("analyzer MAE", [mean_abs(an) for _, _, an in per_run])
        interval("difference, mean", [statistics.fmean(d) for d in diffs])
        interval("difference, MAE", [mean_abs(d) for d in diffs])
