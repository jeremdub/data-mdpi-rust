#!/usr/bin/env python3
"""E5: Section 5.5, from the power-profiler captures of a leaf under traffic.

    python3 check_e5.py                                 # the ten runs
    python3 check_e5.py e5-energy/e5-run1.ppk2          # one capture
    python3 check_e5.py --guard 200 e5-energy/*g200*.ppk2

The leaf runs SCHEDULE=star with NODE_ID=1, so every 101-slot slotframe holds
a receive cell towards the coordinator (slot 0), a dedicated transmit cell
(slot 1), and a shared advertising cell (slot 11). It receives the
coordinator's Enhanced Beacon every BEACON_PERIOD_S in slot 0, sends its own
in slot 11, and sends one acknowledged data frame every SEND_PERIOD_MS in
slot 1.

Each radio burst is placed on the slotframe grid, which is fitted to the
capture, and the slot says which cell it is. A cell that holds a frame costs
more than an empty one, which separates the two. The method does not look at
the width of the guard window, so the same script reads a capture at any guard
time and at any sample rate. Needs NumPy.
"""
import argparse
import glob
import json
import math
import zipfile

import numpy as np

SLOT_S, SLOTFRAME_SLOTS = 0.010, 101
SLOTFRAME = SLOT_S * SLOTFRAME_SLOTS
RX_SLOT, TX_SLOT, ADV_SLOT = 0, 1, 11
GUARD = 2200.0
BURST_UA, MERGE_S, BOOT_UA = 100.0, 200e-6, 1000.0
SCAN_MIN_S, SETTLE_S = 1.0, 1.0
RADIO_MIN_UC = 5.0  # a radio burst against a bare CPU wake
OCCUPIED = 1.08  # a cell that carries a frame, against the empty cell
WAKE_MAX_S, ARM_GAP_S = 1.5e-3, 2e-3
GROUPS = {1: 12.7062, 2: 4.3027, 3: 3.1824, 4: 2.7764, 5: 2.5706, 6: 2.4469,
        7: 2.3646, 8: 2.3060, 9: 2.2622, 10: 2.2281, 11: 2.2010, 12: 2.1788}
SUMMARY = [("join_s", "network entry (s)", 1), ("join_mc", "network entry (mC)", 1),
           ("mean_ua", "mean current (uA)", 2),
           ("slotframe_uc", "charge per slotframe (uC)", 2),
           ("floor_ua", "sleep floor (uA)", 2),
           ("q_cell_uc", "listening cell (uC)", 2),
           ("q_ebrx_uc", "beacon reception (uC)", 2),
           ("q_ebtx_uc", "beacon transmission (uC)", 2),
           ("q_data_uc", "acknowledged data frame (uC)", 2),
           ("share_cell", "share: listening cells (%)", 1),
           ("share_ebrx", "share: beacon receptions (%)", 1),
           ("share_ebtx", "share: beacon transmissions (%)", 1),
           ("share_data", "share: data transmissions (%)", 1),
           ("share_arm", "share: arming wakes (%)", 1),
           ("share_poll", "share: resync polls (%)", 1),
           ("share_floor", "share: sleep floor (%)", 1),
           ("n_data", "data frames sent", 0), ("n_ebtx", "beacons sent", 0),
           ("n_ebrx", "beacons received", 0), ("miss_pct", "cells missed (%)", 2)]


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


def slot_grid(times):
    """Fit the slotframe grid to the radio bursts, and return a slot per burst."""
    t0, period = times[0], SLOTFRAME
    for _ in range(6):
        index = np.round((times - t0) / period)
        offset = (times - t0) - index * period
        hist, edge = np.histogram(offset, bins=np.arange(-period / 2,
                                                         period / 2 + SLOT_S, SLOT_S))
        centre = (edge[np.argmax(hist)] + edge[np.argmax(hist) + 1]) / 2
        keep = np.abs(offset - centre) < SLOT_S / 2
        design = np.vstack([index[keep], np.ones(keep.sum())]).T
        period, t0 = np.linalg.lstsq(design, times[keep], rcond=None)[0]
    offset = (times - t0) - np.round((times - t0) / period) * period
    return np.round(offset / SLOT_S).astype(int) % SLOTFRAME_SLOTS, period


def conf_interval_95(values):
    v = np.asarray(values, float)
    return v.mean(), GROUPS[v.size - 1] * v.std(ddof=1) / math.sqrt(v.size)


def analyse(path, rate):
    current, fs = load(path, rate)
    print("\n%s: %.3f s at %d kHz, %d samples"
          % (path, len(current) / fs, fs // 1000, len(current)))
    found = windows(current, fs)
    duration = (found[:, 1] - found[:, 0]) / fs
    boot = int(np.argmax(current > BOOT_UA)) if (current > BOOT_UA).any() else 0
    scans = np.flatnonzero((duration > SCAN_MIN_S) & (found[:, 1] > boot))
    r, first = {}, 0
    if scans.size:
        entry = current[boot:found[scans[0], 1]]
        first = found[scans[0], 1] + round(SETTLE_S * fs)
        r.update(join_s=len(entry) / fs, join_mc=entry.sum() / fs / 1e3,
                 join_ma=entry.mean() / 1e3)

    steady = current[first:]
    seconds = len(steady) / fs
    charge = steady.sum() / fs
    frames = seconds / SLOTFRAME
    r["mean_ua"] = charge / seconds
    r["slotframe_uc"] = charge / frames
    found = windows(steady, fs)
    duration = (found[:, 1] - found[:, 0]) / fs
    burst = np.array([steady[a:b].sum() / fs for a, b in found])
    r["floor_ua"] = (charge - burst.sum()) / (seconds - duration.sum())
    if "join_s" in r:
        print("  network entry        %.1f s, %.1f mC, %.2f mA (%.0f x the synchronized current)"
              % (r["join_s"], r["join_mc"], r["join_ma"], r["join_ma"] * 1e3 / r["mean_ua"]))
    print("  synchronized         %.1f s (%.0f slotframes)" % (seconds, frames))
    print("    mean current       %.2f uA" % r["mean_ua"])
    print("    charge/slotframe   %.2f uC" % r["slotframe_uc"])
    print("    sleep floor        %.2f uA  (mean between radio operations)" % r["floor_ua"])
    print("    radio-on duty      %.3f %%" % (duration.sum() / seconds * 100))

    radio = burst > RADIO_MIN_UC
    slot = np.full(duration.size, -1)
    slot[radio], period = slot_grid(found[radio, 0] / fs)
    print("    slotframe grid     %.6f s over the capture (%.0f ppm from %.2f s)"
          % (period, (period / SLOTFRAME - 1) * 1e6, SLOTFRAME))

    def split(which):
        """Empty cell against cell with a frame, inside one slot."""
        here = radio & (slot == which)
        if not here.any():
            return here & False, here & False
        reference = np.median(burst[here])
        full = here & (burst > OCCUPIED * reference)
        return here & ~full, full

    empty_rx, ebrx = split(RX_SLOT)
    empty_adv, ebtx = split(ADV_SLOT)
    data = radio & (slot == TX_SLOT)
    other = radio & ~(empty_rx | ebrx | empty_adv | ebtx | data)
    cell = empty_rx | empty_adv

    wake = ~radio & (duration < WAKE_MAX_S)
    cell_starts = found[radio, 0]
    nxt = np.searchsorted(cell_starts, found[:, 1])
    lead = np.full(duration.size, np.inf)
    ok = nxt < cell_starts.size
    lead[ok] = (cell_starts[nxt[ok]] - found[ok, 1]) / fs
    arm = wake & (lead <= ARM_GAP_S)
    poll = wake & ~arm

    groups = [("listening cell, nothing received", cell, "cell"),
              ("Enhanced Beacon reception", ebrx, "ebrx"),
              ("Enhanced Beacon transmission", ebtx, "ebtx"),
              ("acknowledged data transmission", data, "data"),
              ("CPU wake that arms a cell", arm, "arm"),
              ("application resync poll", poll, "poll"),
              ("off-grid radio bursts", other, "other")]
    print("\n  charge census")
    for name, group, key in groups:
        if group.any():
            print("    %-34s n=%4d  %7.3f uC  %5.2f %%  every %6.2f s"
                  % (name, group.sum(), burst[group].mean(),
                     burst[group].sum() / charge * 100, seconds / group.sum()))
        else:
            print("    %-34s n=  0        --            --" % name)
        if key:
            r["share_" + key] = burst[group].sum() / charge * 100 if group.any() else 0.0
            r["q_" + key + "_uc"] = burst[group].mean() if group.any() else float("nan")
            r["n_" + key] = int(group.sum())
    floor_charge = charge - burst.sum()
    print("    %-34s         %7.3f uC  %5.2f %%"
          % ("sleep floor", floor_charge, floor_charge / charge * 100))
    r["share_floor"] = floor_charge / charge * 100

    scheduled = 2 * frames  # slot 0 and slot 11 fire in every slotframe
    opened = (empty_rx | ebrx | empty_adv | ebtx).sum()
    r["miss_pct"] = max(0.0, (scheduled - opened) / scheduled * 100)
    print("    per slotframe: %.2f empty cells, %.3f beacons in, %.3f beacons out, "
          "%.3f data frames" % (cell.sum() / frames, ebrx.sum() / frames,
                                ebtx.sum() / frames, data.sum() / frames))
    print("    receive cells opened: %d of %.0f scheduled (%.2f %% missed)"
          % (opened, scheduled, r["miss_pct"]))
    if ebrx.any():
        gaps = np.diff(found[ebrx, 0]) / fs
        period = np.median(gaps)
        r["eb_missed"] = int(round((gaps / period - 1).clip(0).sum()))
        print("    beacons in: %d, one every %.2f s, longest gap %.2f s (%d missed)"
              % (ebrx.sum(), period, gaps.max(), r["eb_missed"]))
    if data.any() and data.sum() > 2:
        gaps = np.diff(found[data, 0]) / fs
        period = np.median(gaps)
        off = int((gaps < 0.5 * period).sum())
        print("    data frames: %d, one every %.2f s%s"
              % (data.sum(), period,
                 ", plus %d off the period" % off if off else ""))

    if data.any():
        dt = 1e6 / fs
        parts = []
        for a, b in found[data]:
            segment = steady[a:b]
            off = np.flatnonzero(segment < BOOT_UA)
            if off.size == 0:
                continue
            run = max(np.split(off, np.flatnonzero(np.diff(off) > 1) + 1), key=len)
            parts.append((run[0] * dt, len(run) * dt, (len(segment) - run[-1] - 1) * dt,
                          segment[:run[0]].sum() / fs, segment[run[-1] + 1:].sum() / fs))
        p = np.median(np.array(parts), axis=0)
        print("\n  the acknowledged data cell, median of %d" % len(parts))
        print("    frame out             %4.0f us  %6.3f uC" % (p[0], p[3]))
        print("    radio off, turnaround %4.0f us" % p[1])
        print("    acknowledgment in     %4.0f us  %6.3f uC" % (p[2], p[4]))
    return r


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="*")
    ap.add_argument("--rate", type=int, default=100_000)
    ap.add_argument("--guard", type=float, default=GUARD)
    args = ap.parse_args()
    GUARD = args.guard
    runs = [analyse(p, args.rate)
            for p in args.captures or sorted(glob.glob("e5-energy/*.ppk2"))]
    if len(runs) > 1:
        print("\n=== %d runs: mean and 95%% confidence interval ===" % len(runs))
        for key, label, dec in SUMMARY:
            values = [r[key] for r in runs if key in r and not math.isnan(r[key])]
            if len(values) > 1:
                mean, half = conf_interval_95(values)
                print("  %-32s %8.*f   %.*f to %.*f"
                      % (label, dec, mean, dec, mean - half, dec, mean + half))
