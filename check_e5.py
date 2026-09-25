#!/usr/bin/env python3
"""E5: Section 5.5 and Table 9, from a raw power-profiler capture.

    python3 check_e5.py e5-energy/e5-82s.ppk2        # the capture the paper reports
    python3 check_e5.py e5-energy/e5-10s.csv         # the repeat run of Section 5.5
    python3 check_e5.py e5-energy/e5-10s-200us.csv   # the 200 us guard time

One capture at a time. A .ppk2 file is a zip: session.raw holds six bytes per
sample, a float32 current in uA followed by a uint16 of digital-channel state
(unused here). A .csv file is the profiler export, current in its second
column. Needs NumPy.

The phase windows and the charge census below are cut for the 2200 us guard
time. On the 200 us capture a listening cell is 410 us instead of 2410 us, so
only the mean current and the sleep floor are meaningful there.
"""

import sys
import zipfile

import numpy as np

FS = 100_000       # PPK2 sample rate, Hz
START = 12.0       # s, analysis window starts here; the node joins at 11.4 s
SLOTFRAME = 1.01   # s, 101 timeslots of 10 ms
GUARD = 2200.0     # us, macTsRxWait
PREWARM = 427.2    # us, HFXO_PREWARM_LEAD_TICKS = 14 sleep ticks

T_RXEN = 129.625   # us, the programmed radio ramp
T_SHR = 160.0      # us, preamble and start-of-frame delimiter
T_FRAMESTART = 52.713  # us, the radio reports FRAMESTART this late after the RMARKER

# Table 9: phases of one listening cell, in us relative to the CPU wake that
# programs the radio. The crystal starts 135 us before that wake and is ready
# at +210; the radio ramp begins at +310 and ends one T_RXEN later, at +440.
PHASES = [("CPU wakes, arms the slot", -1500, -950),
          ("sleeps, everything off", -950, -140),
          ("crystal starts, CPU asleep", -140, 0),
          ("CPU programs the radio, crystal warming", 0, 310),
          ("radio ramp", 310, 440),
          ("guard window, radio alone", 440, 2850),
          ("CPU closes the slot", 2850, 3400)]
CRYSTAL = (-140, 0)     # window where the crystal runs and nothing else does


def load(path):
    """Supply current in uA."""
    if path.endswith(".csv"):
        return np.loadtxt(path, delimiter=",", skiprows=1, usecols=1)
    archive = zipfile.ZipFile(path)
    raw = archive.read("session.raw")
    archive.close()
    raw = raw[:len(raw) - len(raw) % 6]
    sample = np.dtype([("current", "<f4"), ("digital", "<u2")])
    return np.frombuffer(raw, dtype=sample)["current"]


def windows(current, level=100.0, merge=20):
    """[start, stop] sample index of each window above level.

    Gaps shorter than `merge` samples are part of the same window.
    """
    above = current > level
    starts = np.flatnonzero(~above[:-1] & above[1:]) + 1
    stops = np.flatnonzero(above[:-1] & ~above[1:]) + 1
    if stops[0] < starts[0]:
        stops = stops[1:]
    starts = starts[:len(stops)]

    out = [[starts[0], stops[0]]]
    for start, stop in zip(starts[1:], stops[1:]):
        if start - out[-1][1] < merge:
            out[-1][1] = stop
        else:
            out.append([start, stop])
    return np.array(out)


def sample_index(offset_us):
    """Where an offset from the CPU wake falls in the averaged cell."""
    return int((1800 + offset_us) / 10)


if len(sys.argv) > 1:
    path = sys.argv[1]
else:
    path = "e5-energy/e5-82s.ppk2"
current = load(path)

# Network entry is one long block at milliamps; the first tenth of a second
# whose average falls below 1 mA is where it ends.
smooth = np.convolve(current, np.ones(FS // 10) / (FS // 10), "same")
join = np.flatnonzero(smooth < 1000)[0] / FS

if join > 1.0:
    first = int(START * FS)
else:
    first = 0
steady = current[first:]
seconds = len(steady) / FS
charge = steady.sum() / FS
mean_current = charge / seconds

print("capture %.3f s, %d samples\n" % (len(current) / FS, len(current)))

if join > 1.0:
    entry = current[:int(join * FS)]
    entry_charge = entry.sum() / FS
    print("Network entry")
    print("  ends at              %.1f s" % join)
    print("  mean current         %.0f uA" % entry.mean())
    print("  charge               %.1f mC" % (entry_charge / 1e3))
    print("  = steady state for   %.0f s (%.0f min)"
          % (entry_charge / mean_current, entry_charge / mean_current / 60))
    print("  ratio to steady      %.0f x\n" % (entry.mean() / mean_current))

floor = np.median(steady)
per_second = []
for i in range(0, len(steady) - FS, FS):
    per_second.append(np.median(steady[i:i + FS]))

print("Section 5.5, synchronized operation (%.1f s, %.1f slotframes)"
      % (seconds, seconds / SLOTFRAME))
print("  mean current         %.2f uA" % mean_current)
print("  sleep floor          %.2f uA" % floor)
print("  floor spread         +-%.3f uA" % np.std(per_second))
print("  duty above 100 uA    %.3f %%" % ((steady > 100).mean() * 100))
print("  charge per slotframe %.2f uC" % (charge / (seconds / SLOTFRAME)))

# Sort the windows above 100 uA by how long they last.
found = windows(steady) + first
duration_us = (found[:, 1] - found[:, 0]) / FS * 1e6
listen = found[(duration_us > 3000) & (duration_us < 3400)]
beacon = found[duration_us > 4000]
arm = found[duration_us <= 380]
timer = found[(duration_us > 380) & (duration_us < 600)]

print("\n  charge census")
counted = 0.0
groups = [("listening cell, nothing received", listen),
          ("beacon reception", beacon),
          ("CPU wake that arms a slot", arm),
          ("application timer", timer)]
for name, group in groups:
    charges = []
    for start, stop in group:
        charges.append(current[start:stop].sum() / FS)
    counted += sum(charges)
    if not charges:
        # No window of this width in the capture: a guard time other than
        # 2200 us moves the listening cell out of the classifier's range.
        print("    %-34s n=  0        --            --" % name)
        continue
    print("    %-34s n=%3d  %7.3f uC  %5.2f %%"
          % (name, len(group), sum(charges) / len(charges),
             sum(charges) / charge * 100))
floor_charge = floor * seconds
print("    %-34s        %7.3f uC  %5.2f %%"
      % ("sleep floor", floor_charge, floor_charge / charge * 100))
print("    %-34s                        %5.2f %%"
      % ("residual", (charge - counted - floor_charge) / charge * 100))

# One listening cell, averaged. The window is 180 samples before the CPU wake
# and 420 after it, so one sample is 10 us.
starts = listen[:, 0]
if len(starts) == 0:
    # Table 9 and everything below it need listening cells of the width the
    # 2200 us guard time gives. Another guard time has none, and the figures
    # above are all this capture supports.
    print("\nno listening cell of the expected width: Table 9 needs a capture "
          "at the 2200 us guard time")
    sys.exit(0)
shape = []
for start in starts:
    shape.append(current[start - 180:start + 420])
cell = np.array(shape).mean(axis=0)

print("\nTable 9, one listening cell averaged over %d cells" % len(starts))
phase_charges = []
for name, begin, end in PHASES:
    piece = cell[sample_index(begin):sample_index(end)]
    phase_charges.append(piece.sum() * 10e-6)
cell_total = sum(phase_charges)
for i in range(len(PHASES)):
    name, begin, end = PHASES[i]
    print("    %-39s %5d us  %7.3f uC  %5.1f %%"
          % (name, end - begin, phase_charges[i],
             phase_charges[i] / cell_total * 100))
print("    %-39s %5d us  %7.3f uC"
      % ("total", PHASES[-1][2] - PHASES[0][1], cell_total))
radio_off = phase_charges[0] + phase_charges[3] + phase_charges[6]
print("    radio off for %.3f uC of %.3f uC = %.1f %% (bounds the CPU and "
      "crystal share)" % (radio_off, cell_total, radio_off / cell_total * 100))

# The two radio rows against the driver: the receiver is armed one ramp and one
# SHR before the earliest RMARKER the window accepts, and stays on until the
# FRAMESTART that a frame at the latest acceptable RMARKER would report.
ramp_us = PHASES[4][2] - PHASES[4][1]
window_us = PHASES[5][2] - PHASES[5][1]
print("    ramp %d us against %.3f us programmed" % (ramp_us, T_RXEN))
print("    window %d us against %.0f + %.0f + %.3f = %.3f us"
      % (window_us, T_SHR, GUARD, T_FRAMESTART, T_SHR + GUARD + T_FRAMESTART))

crystal = cell[sample_index(CRYSTAL[0]):sample_index(CRYSTAL[1])].mean()
before = cell[sample_index(-320):sample_index(-150)].mean()
prewarm_charge = PREWARM * 1e-6 * crystal
print("\n  the crystal pre-warm")
print("    sleep floor          %.1f uA" % before)
print("    crystal alone        %.1f uA over %d us"
      % (crystal, CRYSTAL[1] - CRYSTAL[0]))
print("    lead of %.1f us     %.4f uC = %.2f %% of the cell"
      % (PREWARM, prewarm_charge, prewarm_charge / cell_total * 100))
print("    crystal start to ramp 445 us = 345 us start-up + 100 us lead")
print("      (the paper measures that lead as 98.0 us with the analyzer)")

# The plateau of each cell, +800 to +2700 us after the wake.
plateau = []
for start in starts:
    plateau.append(current[start + 80:start + 270])
plateau = np.array(plateau)
average = plateau.mean(axis=0)
noise = plateau.std(axis=1).mean()

peaks = []
for start, stop in listen:
    peaks.append(current[start:stop].max())
cpu_current = np.mean(peaks) - average.mean()
# A burst of the CPU would lift the average by cpu_current for as long as it
# runs
burst_us = 3 * average.std() * 45.0 / cpu_current

print("\n  the CPU during the guard window")
print("    plateau level        %.1f uA" % average.mean())
print("    instrument noise     %.1f uA per sample" % noise)
print("    predicted sd of mean %.1f uA" % (noise / np.sqrt(len(plateau))))
print("    observed sd of mean  %.1f uA" % average.std())
print("    CPU increment        +%.0f uA (cell peak minus plateau)" % cpu_current)
print("    3-sigma CPU bound    %.2f us in %.0f us  ->  idle > %.2f %%"
      % (burst_us, GUARD, 100 * (1 - burst_us / GUARD)))
