#!/usr/bin/env python3
"""E3b and E3c: the Contiki-NG rows of Table 8 and Supplementary Table S9.

    python3 check_e3.py                       # every e3b-run* and e3c-run*
    python3 check_e3.py --latex               # the rows of Table S9

Each beacon gives two errors after the 600 s warm-up: the analyzer interval from
the leaf's receive-window event (Channel 11) to the coordinator's frame start
(Channel 9), referred to the median of its run, and the err_ns the leaf logs.
The two are paired by time, so a lost beacon or a dropped log line costs one
sample. Intervals are 95% over the per-run mean absolute errors (Student's t).
"""
import argparse
import bisect
import csv
import math
import statistics
from pathlib import Path

WARMUP_S = 600.0
SAME_SLOT_S = 0.004
TX_CHANNEL, RX_CHANNEL = 0, 2
PAIR_TOL_S = 0.30
T975 = {1: 12.7062, 2: 4.3027, 3: 3.1824, 4: 2.7764, 5: 2.5706, 6: 2.4469, 7: 2.3646,
        8: 2.3060, 9: 2.2622, 10: 2.2281, 11: 2.2010, 12: 2.1788, 13: 2.1604, 14: 2.1448}


def rising(path, channel):
    out, prev = [], None
    with open(path) as f:
        rows = csv.reader(f)
        next(rows)
        for row in rows:
            v = int(row[1 + channel])
            if prev == 0 and v == 1:
                out.append(float(row[0]))
            prev = v
    return out


def analyzer_beacons(d):
    tx = rising(d / "logic-analyzer.csv", TX_CHANNEL)
    rx = rising(d / "logic-analyzer.csv", RX_CHANNEL)
    out = []
    for b in tx:
        i = bisect.bisect_left(rx, b)
        if i and b - rx[i - 1] < SAME_SLOT_S:
            out.append((b, (rx[i - 1] - b) * 1e6))
    return out


def software_samples(d):
    with open(d / "interop_contiki-coord_contiki-leaf.csv") as f:
        return sorted((float(r["time_s"]), int(r["asn"]), int(r["err_ns"]))
                      for r in csv.DictReader(f) if r["src"] == "frame")


def offset(la, sw, binw=0.05):
    swt = [s[0] for s in sw]
    hist = {}
    for lt, _ in la:
        i = bisect.bisect_left(swt, lt - 12.0)
        while i < len(swt) and swt[i] < lt + 12.0:
            k = round((swt[i] - lt) / binw)
            hist[k] = hist.get(k, 0) + 1
            i += 1
    if not hist:
        return 0.0
    k = max(hist, key=hist.get)
    vals = []
    for lt, _ in la:
        i = bisect.bisect_left(swt, lt + (k - 1.5) * binw)
        while i < len(swt) and swt[i] <= lt + (k + 1.5) * binw:
            vals.append(swt[i] - lt)
            i += 1
    return statistics.median(vals)


def pair(la, sw):
    off = offset(la, sw)
    swt = [s[0] for s in sw]
    used, recent, out = set(), [], []
    for lt, interval in la:
        target = lt + (off if len(recent) < 10 else statistics.median(recent[-40:]))
        i = bisect.bisect_left(swt, target)
        best = bestd = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(swt) and j not in used and (bestd is None or abs(swt[j] - target) < bestd):
                best, bestd = j, abs(swt[j] - target)
        if best is None or bestd > PAIR_TOL_S:
            continue
        used.add(best)
        recent.append(swt[best] - lt)
        out.append((lt, interval, sw[best][2]))
    return out


def stats(xs):
    return (len(xs), statistics.fmean(map(abs, xs)),
            statistics.stdev(xs) if len(xs) > 1 else 0.0, max(map(abs, xs)))


def corr(a, b):
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db) if da and db else 0.0


def ci95(values):
    m = statistics.fmean(values)
    h = T975[len(values) - 1] * statistics.stdev(values) / math.sqrt(len(values))
    return m - h, m + h


def summary(name, la_e, sw_e):
    return {"name": name, "n": len(la_e), "la": stats(la_e), "sw": stats(sw_e),
            "zero": 100.0 * sum(1 for b in sw_e if b == 0.0) / len(sw_e),
            "r": corr(la_e, sw_e), "la_err": la_e, "sw_err": sw_e}


def analyse_run(d):
    la = analyzer_beacons(d)
    med = statistics.median(x for _, x in la)
    kept = [(i - med, e / 1000.0) for t, i, e in pair(la, software_samples(d)) if t >= WARMUP_S]
    return summary(d.name, [a for a, _ in kept], [b for _, b in kept])


def report(exp, runs, latex):
    pooled = summary("Ten runs", [x for r in runs for x in r["la_err"]],
                     [x for r in runs for x in r["sw_err"]])
    if latex:
        print("% " + exp)
        for i, r in enumerate(runs, 1):
            s, l = r["sw"], r["la"]
            print(f"{i:<2} & {r['n']} & {s[1]:.3f} & {s[2]:.3f} & {s[3]:.3f} & "
                  f"{l[1]:.3f} & {l[2]:.3f} & {l[3]:.3f} & {r['zero']:.1f} \\\\")
        s, l = pooled["sw"], pooled["la"]
        n = f"{pooled['n']:,}".replace(",", r"\,")
        print(r"\addlinespace")
        print(f"Ten runs & {n} & {s[1]:.3f} & {s[2]:.3f} & {s[3]:.3f} & "
              f"{l[1]:.3f} & {l[2]:.3f} & {l[3]:.3f} & {pooled['zero']:.1f} \\\\")
        print(f"% Table 8: software {s[1]:.3f} {s[2]:.3f} | analyzer {l[1]:.3f} {l[2]:.3f}\n")
        return
    print(f"=== {exp} ===")
    print(f"{'run':<5}{'n':<7}{'sw MAE':>8}{'sw sd':>8}{'sw max':>8}"
          f"{'la MAE':>9}{'la sd':>8}{'la max':>8}{'zero%':>7}{'r':>7}")
    for i, r in enumerate(runs + [pooled], 1):
        s, l = r["sw"], r["la"]
        label = "all" if r is pooled else str(i)
        print(f"{label:<5}{r['n']:<7}{s[1]:8.3f}{s[2]:8.3f}{s[3]:8.3f}"
              f"{l[1]:9.3f}{l[2]:8.3f}{l[3]:8.3f}{r['zero']:7.1f}{r['r']:7.3f}")
    if len(runs) > 2:
        for key, name in (("la", "analyzer"), ("sw", "software")):
            lo, hi = ci95([r[key][1] for r in runs])
            print(f"  95% CI of the {name} MAE over the runs: {lo:.4f} to {hi:.4f}")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default="e3b-e3c-sync_error_contiki")
    ap.add_argument("--latex", action="store_true")
    args = ap.parse_args()
    for exp in ("e3b", "e3c"):
        dirs = sorted((d for d in Path(args.root).glob(exp + "-run*") if d.is_dir()),
                      key=lambda d: int(d.name.rsplit("run", 1)[1]))
        if dirs:
            report(exp, [analyse_run(d) for d in dirs], args.latex)
