#!/usr/bin/env python3
"""E5: Section 5.5 and Table 10, from the power-profiler captures.

    python3 check_e5.py                                            # the ten runs
    python3 check_e5.py e5-energy/ppk2-100khz-80sec.ppk2           # Table 10
    python3 check_e5.py --guard 200 e5-energy/ppk2-100khz-80sec-guard200us.ppk2
    python3 check_e5.py e5-energy/e5-82s.ppk2                      # before the timer fix

A .ppk2 file is a zip: metadata.json gives the sample rate, session.raw six bytes
per sample (float32 current in uA, uint16 digital state). A .csv export is read
at --rate. Below 100 kHz the profiler logs the mean of each period: charges hold
at any rate, but Table 10 needs 100 kHz. --guard moves the cell class and the
phases to another guard time. Needs NumPy.
"""
import argparse
import glob
import json
import math
import zipfile

import numpy as np

SLOTFRAME = 1.01
GUARD = 2200.0
PREWARM = 427.2
T_RXEN, T_SHR, T_FRAMESTART = 129.625, 160.0, 52.713
BURST_UA, MERGE_S, BOOT_UA = 100.0, 200e-6, 1000.0
SCAN_MIN_S, SETTLE_S = 1.0, 1.0
CELL_S = (2.5e-3, 4.5e-3)
RX_MAX_S, WAKE_MAX_S, ARM_GAP_S = 10e-3, 1.5e-3, 2e-3
PHASES = [("CPU wakes, arms the slot", -1500, -950),
          ("sleeps, everything off", -950, -140),
          ("crystal starts, CPU asleep", -140, 0),
          ("CPU programs the radio, crystal warming", 0, 310),
          ("radio ramp", 310, 440),
          ("guard window, radio alone", 440, 2850),
          ("CPU closes the slot", 2850, 3400)]
CRYSTAL = (-140, 0)
PLATEAU = (800, 2700)
STEP_US = 10.0
T975 = {1: 12.7062, 2: 4.3027, 3: 3.1824, 4: 2.7764, 5: 2.5706, 6: 2.4469, 7: 2.3646,
        8: 2.3060, 9: 2.2622, 10: 2.2281, 11: 2.2010, 12: 2.1788, 13: 2.1604, 14: 2.1448}
SUMMARY = [("join_s", "network entry (s)", 1), ("join_mc", "network entry (mC)", 1),
           ("mean_ua", "mean current (uA)", 2), ("slotframe_uc", "charge per slotframe (uC)", 2),
           ("floor_ua", "sleep floor (uA)", 2), ("q_cell_uc", "listening cell (uC)", 2),
           ("q_beacon_uc", "beacon reception (uC)", 2),
           ("cells_per_slotframe", "listening cells per slotframe", 2),
           ("share_cell", "share: listening cells (%)", 1),
           ("share_beacon", "share: beacon receptions (%)", 1),
           ("share_arm", "share: arming wakes (%)", 1), ("share_poll", "share: resync polls (%)", 1),
           ("share_floor", "share: sleep floor (%)", 1)]


def set_guard(guard_us):
    global GUARD, PHASES, CELL_S, WAKE_MAX_S, ARM_GAP_S, PLATEAU
    shift = guard_us - GUARD
    GUARD = guard_us

    def move(x):
        if x <= -950:
            return x + shift / 2
        return x + shift if x > 440 else x

    PHASES = [(name, move(b), move(e)) for name, b, e in PHASES]
    CELL_S = (CELL_S[0] + shift * 1e-6, CELL_S[1] + shift * 1e-6)
    WAKE_MAX_S = min(WAKE_MAX_S, CELL_S[0])
    ARM_GAP_S = ARM_GAP_S - shift / 2 * 1e-6
    PLATEAU = (min(PLATEAU[0], 540), PLATEAU[1] + shift)


def load(path, rate):
    if path.endswith(".csv"):
        return np.loadtxt(path, delimiter=",", skiprows=1, usecols=1), rate
    with zipfile.ZipFile(path) as archive:
        meta = json.loads(archive.read("metadata.json"))
        raw = archive.read("session.raw")
    raw = raw[:len(raw) - len(raw) % 6]
    sample = np.dtype([("current", "<f4"), ("digital", "<u2")])
    return (np.frombuffer(raw, dtype=sample)["current"].astype(np.float64),
            int(meta["metadata"]["samplesPerSecond"]))


def windows(current, fs, level=BURST_UA, merge_s=MERGE_S):
    above = current > level
    edges = np.flatnonzero(np.diff(above.astype(np.int8)))
    starts = list(edges[~above[edges]] + 1)
    stops = list(edges[above[edges]] + 1)
    if above[0]:
        starts.insert(0, 0)
    if above[-1]:
        stops.append(current.size)
    merge = max(1, round(merge_s * fs))
    out = []
    for start, stop in zip(starts, stops):
        if out and start - out[-1][1] < merge:
            out[-1][1] = stop
        else:
            out.append([start, stop])
    return np.array(out, dtype=np.int64).reshape(-1, 2)


def average_cell(current, fs, starts):
    dt = 1e6 / fs
    pre = int(math.ceil(-PHASES[0][1] * 1.2 / dt)) + 1
    post = int(math.ceil(PHASES[-1][2] * 1.2 / dt)) + 2
    floor = np.median(current)
    level = np.median([current[i + 1] for i in starts if i + 1 < current.size])
    grid = np.arange(PHASES[0][1] * 1.2, PHASES[-1][2] * 1.2 + 1e-9, STEP_US)
    total, n = np.zeros(grid.size), 0
    for i in starts:
        if i - pre < 0 or i + post > current.size:
            continue
        share = np.clip((current[i] - floor) / (level - floor), 0.0, 1.0)
        t = (np.arange(-pre, post) - (1.0 - share)) * dt
        total += np.interp(grid, t, current[i - pre:i + post])
        n += 1
    return grid, total / n, n


def ci95(values):
    v = np.asarray(values, float)
    return v.mean(), T975[v.size - 1] * v.std(ddof=1) / math.sqrt(v.size)


def analyse(path, rate):
    current, fs = load(path, rate)
    print("\n%s: %.3f s at %d kHz, %d samples" % (path, len(current) / fs, fs // 1000, len(current)))
    found = windows(current, fs)
    duration = (found[:, 1] - found[:, 0]) / fs
    boot = int(np.argmax(current > BOOT_UA)) if (current > BOOT_UA).any() else 0
    scans = np.flatnonzero((duration > SCAN_MIN_S) & (found[:, 1] > boot))
    r, first = {}, 0
    if scans.size:
        entry = current[boot:found[scans[0], 1]]
        first = found[scans[0], 1] + round(SETTLE_S * fs)
        r.update(join_s=len(entry) / fs, join_mc=entry.sum() / fs / 1e3, join_ma=entry.mean() / 1e3)

    steady = current[first:]
    seconds = len(steady) / fs
    charge = steady.sum() / fs
    r["mean_ua"] = charge / seconds
    r["slotframe_uc"] = charge / (seconds / SLOTFRAME)
    found = windows(steady, fs)
    duration = (found[:, 1] - found[:, 0]) / fs
    burst = np.array([steady[a:b].sum() / fs for a, b in found])
    r["floor_ua"] = (charge - burst.sum()) / (seconds - duration.sum())
    if "join_s" in r:
        print("  network entry        %.1f s, %.1f mC, %.2f mA (%.0f x the synchronized current)"
              % (r["join_s"], r["join_mc"], r["join_ma"], r["join_ma"] * 1e3 / r["mean_ua"]))
    print("  synchronized         %.1f s (%.0f slotframes)" % (seconds, seconds / SLOTFRAME))
    print("    mean current       %.2f uA" % r["mean_ua"])
    print("    charge/slotframe   %.2f uC" % r["slotframe_uc"])
    print("    sleep floor        %.2f uA  (mean between radio operations)" % r["floor_ua"])
    print("    median of capture  %.2f uA" % np.median(steady))
    print("    radio-on duty      %.3f %%" % (duration.sum() / seconds * 100))

    cell = (duration >= CELL_S[0]) & (duration < CELL_S[1])
    beacon = (duration >= CELL_S[1]) & (duration < RX_MAX_S)
    wake = duration < WAKE_MAX_S
    cell_starts = found[cell | beacon, 0]
    nxt = np.searchsorted(cell_starts, found[:, 1])
    gap = np.full(duration.size, np.inf)
    ok = nxt < cell_starts.size
    gap[ok] = (cell_starts[nxt[ok]] - found[ok, 1]) / fs
    arm = wake & (gap <= ARM_GAP_S)
    poll = wake & ~arm
    groups = [("listening cell, nothing received", cell, "cell"), ("beacon reception", beacon, "beacon"),
              ("CPU wake that arms a cell", arm, "arm"), ("application resync poll", poll, "poll"),
              ("other bursts", ~(cell | beacon | wake), None)]
    print("\n  charge census")
    for name, group, key in groups:
        if group.any():
            print("    %-34s n=%4d  %7.3f uC  %5.2f %%"
                  % (name, group.sum(), burst[group].mean(), burst[group].sum() / charge * 100))
        else:
            print("    %-34s n=  0        --            --" % name)
        if key:
            r["share_" + key] = burst[group].sum() / charge * 100 if group.any() else float("nan")
    floor_charge = charge - burst.sum()
    print("    %-34s         %7.3f uC  %5.2f %%" % ("sleep floor", floor_charge, floor_charge / charge * 100))
    r["share_floor"] = floor_charge / charge * 100
    r["q_cell_uc"] = burst[cell].mean() if cell.any() else float("nan")
    r["q_beacon_uc"] = burst[beacon].mean() if beacon.any() else float("nan")
    r["cells_per_slotframe"] = cell.sum() / (seconds / SLOTFRAME)
    r["beacons_per_slotframe"] = beacon.sum() / (seconds / SLOTFRAME)
    print("    per slotframe: %.2f listening cells and %.2f beacon receptions"
          % (r["cells_per_slotframe"], r["beacons_per_slotframe"]))

    if not cell.any():
        print("\n  no listening cell of the expected width: check --guard")
        return r
    if fs < 100_000:
        print("\n  Table 10 needs a capture at 100 kHz: at %d kHz the profiler averages each %.0f us,\n"
              "  which is wider than the %.0f us radio ramp and the %.0f us crystal phase."
              % (fs // 1000, 1e6 / fs, PHASES[4][2] - PHASES[4][1], CRYSTAL[1] - CRYSTAL[0]))
        return r

    grid, shape, n = average_cell(current, fs, found[cell, 0] + first)
    charges = [shape[(grid >= b) & (grid < e)].sum() * STEP_US * 1e-6 for _, b, e in PHASES]
    total = sum(charges)
    print("\nTable 10, one listening cell averaged over %d cells" % n)
    for (name, b, e), q in zip(PHASES, charges):
        print("    %-39s %5d us  %7.3f uC  %5.1f %%" % (name, e - b, q, q / total * 100))
    print("    %-39s %5d us  %7.3f uC" % ("total", PHASES[-1][2] - PHASES[0][1], total))
    radio_off = charges[0] + charges[3] + charges[6]
    print("    radio off for %.3f uC of %.3f uC = %.1f %% (bounds the CPU and crystal share)"
          % (radio_off, total, radio_off / total * 100))
    print("    ramp %d us against %.3f us programmed" % (PHASES[4][2] - PHASES[4][1], T_RXEN))
    print("    window %d us against %.0f + %.0f + %.3f = %.3f us"
          % (PHASES[5][2] - PHASES[5][1], T_SHR, GUARD, T_FRAMESTART, T_SHR + GUARD + T_FRAMESTART))
    plateau = shape[(grid >= PLATEAU[0]) & (grid < PLATEAU[1])].mean()
    ramp_at = PHASES[4][1]
    lo = shape[(grid >= ramp_at - 60) & (grid < ramp_at)].mean()
    rise = (shape - lo) / (plateau - lo)
    m = (grid > ramp_at - 10) & (grid < PHASES[5][1] + 200)
    print("    plateau %.1f uA; ramp rises 10%% to 90%% in %.0f us"
          % (plateau, grid[m][np.argmax(rise[m] > 0.9)] - grid[m][np.argmax(rise[m] > 0.1)]))

    crystal = shape[(grid >= CRYSTAL[0]) & (grid < CRYSTAL[1])].mean()
    prewarm = PREWARM * 1e-6 * crystal
    print("\n  the crystal pre-warm")
    print("    sleep floor          %.1f uA" % shape[(grid >= -320) & (grid < -150)].mean())
    print("    crystal alone        %.1f uA over %d us" % (crystal, CRYSTAL[1] - CRYSTAL[0]))
    print("    lead of %.1f us     %.4f uC = %.2f %% of the cell" % (PREWARM, prewarm, prewarm / total * 100))
    return r


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="*")
    ap.add_argument("--rate", type=int, default=100_000)
    ap.add_argument("--guard", type=float, default=GUARD)
    args = ap.parse_args()
    if args.guard != GUARD:
        set_guard(args.guard)
    runs = [analyse(p, args.rate) for p in args.captures or sorted(glob.glob("e5-energy/ppk2-2026*.ppk2"))]
    if len(runs) > 1:
        print("\n=== %d runs: mean and 95%% confidence interval ===" % len(runs))
        for key, label, dec in SUMMARY:
            values = [r[key] for r in runs if key in r and not math.isnan(r[key])]
            if len(values) > 1:
                mean, half = ci95(values)
                print("  %-32s %8.*f   %.*f to %.*f" % (label, dec, mean, dec, mean - half, dec, mean + half))
