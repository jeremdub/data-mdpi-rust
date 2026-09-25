#!/usr/bin/env python3
"""E1: Table 5, from the raw Logic 2 exports.

    python3 check_e1.py e1-determinism/run1 e1-determinism/run2 e1-determinism/run3

A run folder holds coordinator-logic_analyzer.csv and leaf-logic_analyzer.csv.
Values are printed per run and then over all the runs pooled, which is the
column the paper reports.
"""

import csv
import statistics
import sys

# Pin order of the tsch-ramp-trace export, one letter per exported channel.
# S = crystal start, C = crystal ready, T = transmitter ready,
# R = receiver ready, E = radio enable, P = last bit on air,
# F = frame start (a level: rise is F, fall is f).
NAMES = ["S", "C", "T", "R", "E", "P", "F"]

# PHY and driver constants, in microseconds.
SHR = 160.0        # preamble and start-of-frame delimiter
PHR = 32.0         # PHY header
OCTET = 32.0       # one octet on air
RAMP = 129.625     # programmed radio ramp
TX_FS = 9.625      # transmit frame start fires this late after the RMARKER
RX_FS = 212.713    # receive frame start fires this late after the first chip
KA_PSDU = 23       # keep-alive PSDU length in octets, FCS included
ACK_WAIT = 400.0   # macTsAckWait, the width of the acknowledgment window

WARMUP_S = 600.0   # discarded at the start of every run
GAP_US = 8000.0    # silence that separates two radio operations
COORD_PAIR = 30000.0   # the coordinator's two receive cells, three timeslots apart
LEAF_PAIR = 50000.0    # the leaf's two receive cells, five timeslots apart


def read_edges(path):
    """Every pin change of one export, as (time in microseconds, letter)."""
    edges = []
    previous = None
    handle = open(path)
    rows = csv.reader(handle)
    next(rows)
    for row in rows:
        time = float(row[0]) * 1e6
        levels = []
        for value in row[1:8]:
            levels.append(int(value))
        if previous is not None:
            for i in range(7):
                if previous[i] == levels[i]:
                    continue
                letter = NAMES[i]
                if letter == "F" and levels[i] == 0:
                    letter = "f"
                edges.append((time, letter))
        previous = levels
    handle.close()
    return edges


def split_episodes(edges):
    """Group the edges into radio operations. A long silence starts a new one."""
    episodes = []
    current = [edges[0]]
    for edge in edges[1:]:
        if edge[0] - current[-1][0] > GAP_US:
            episodes.append(current)
            current = [edge]
        else:
            current.append(edge)
    episodes.append(current)
    return episodes


def new_samples():
    return {
        "spacing": [],     # (1) distance between the two receive cells
        "lead": [],        # (2) crystal ready before the ramp starts
        "ramp": [],        # (3) radio ramp
        "margin": [],      # (4) transmitter ready before the launch
        "listen": [],      # (5) last data bit on air to receiver ready
        "launch": [],      # (6) transmitter ready to the ack on air
        "ack": [],         # (7) last data bit on air to the ack RMARKER
        "window": [],      # where the ack falls inside its window
        "keep_alives": 0,
        "acked": 0,
    }


def measure(path, pair):
    """All samples of one capture. `pair` is the nominal cell spacing."""
    s = new_samples()
    enables = []
    for episode in split_episodes(read_edges(path)):
        if episode[0][0] < WARMUP_S * 1e6:
            continue

        signature = ""
        t = []
        for time, letter in episode:
            signature += letter
            t.append(time)

        if signature == "SCER" or signature == "SCERFPf":
            # A receive cell: idle, or a frame received without an ack.
            crystal_ready = t[1]
            enable = t[2]
            ready = t[3]
            s["lead"].append(enable - crystal_ready)
            s["ramp"].append(ready - enable)
            enables.append(enable)

        elif signature == "SCTEFfP":
            # A transmit cell, nothing expected back (a beacon).
            crystal_ready = t[1]
            tx_ready = t[2]
            launch = t[3]
            ramp_start = tx_ready - RAMP
            s["lead"].append(ramp_start - crystal_ready)
            s["margin"].append(launch - tx_ready)

        elif signature == "SCTEFfPERFPf" or signature == "SCTEFfPER":
            # The leaf: a keep-alive sent, then the acknowledgment window.
            crystal_ready = t[1]
            tx_ready = t[2]
            launch = t[3]
            last_data_bit = t[6]
            ack_enable = t[7]
            ready = t[8]

            ramp_start = tx_ready - RAMP
            s["lead"].append(ramp_start - crystal_ready)
            s["margin"].append(launch - tx_ready)
            s["ramp"].append(ready - ack_enable)
            enables.append(ack_enable)
            s["keep_alives"] += 1

            listen = ready - last_data_bit
            s["listen"].append(listen)
            if signature == "SCTEFfPERFPf":
                # The window is armed one SHR before the earliest RMARKER it
                # must accept, so the window opens one SHR after the ramp ends.
                ack_frame_start = t[9]
                ack_rmarker = ack_frame_start - RX_FS + SHR
                window_opens = last_data_bit + listen + SHR
                s["ack"].append(ack_rmarker - last_data_bit)
                s["window"].append(ack_rmarker - window_opens)
                s["acked"] += 1

        elif signature == "SCERFPfETFfP":
            # The coordinator: a keep-alive received, then the ack sent.
            crystal_ready = t[1]
            enable = t[2]
            ready = t[3]
            data_frame_start = t[4]
            tx_ready = t[8]
            ack_frame_start = t[9]

            s["lead"].append(enable - crystal_ready)
            s["ramp"].append(ready - enable)
            enables.append(enable)

            ack_rmarker = ack_frame_start - TX_FS
            s["launch"].append(ack_rmarker - tx_ready)
            # The last bit of the received frame, from its first chip on air.
            data_rmarker = data_frame_start - RX_FS
            last_data_bit = data_rmarker + SHR + PHR + KA_PSDU * OCTET
            s["ack"].append(ack_rmarker - last_data_bit)

    # The two receive cells of one slotframe. Any other pair is a different
    # distance and is dropped here.
    for first, second in zip(enables, enables[1:]):
        gap = second - first
        if abs(gap - pair) < 5.0:
            s["spacing"].append(gap)
    return s


def row(tag, label, values, nominal):
    if not values:
        return
    middle = statistics.median(values)
    sd_ns = statistics.pstdev(values) * 1000.0
    spread_ns = (max(values) - min(values)) * 1000.0
    print("%3s  %-30s%8d%14.3f%10.1f%12.1f%12s"
          % (tag, label, len(values), middle, sd_ns, spread_ns, nominal))


def report(title, coord, leaf):
    print("\n=== %s" % title)
    print("%3s  %-30s%8s%14s%10s%12s%12s"
          % ("", "Quantity (us)", "n", "Median", "sd (ns)", "Range (ns)", "Nominal"))

    row("1", "Slot spacing, coord.", coord["spacing"], "30000")
    row("1", "Slot spacing, leaf", leaf["spacing"], "50000")
    row("2", "Crystal lead, coord.", coord["lead"], "> 0")
    row("2", "Crystal lead, leaf", leaf["lead"], "> 0")
    row("3", "Radio ramp, coord.", coord["ramp"], "129.625")
    row("3", "Radio ramp, leaf", leaf["ramp"], "129.625")
    row("4", "Launch margin, coord.", coord["margin"], "16")
    row("4", "Launch margin, leaf", leaf["margin"], "16")
    row("5", "RxAckDelay - SHR, leaf", leaf["listen"], "640")
    row("6", "Ack launch, coord.", coord["launch"], "--")
    row("7", "TxAckDelay, leaf", leaf["ack"], "1000")
    row("7", "TxAckDelay, coord.", coord["ack"], "1000")

    # Rows (2), (3) and (4) are reported over the two boards pooled, so their
    # sample counts are the sum of the two lines above.
    print()
    for tag, key in [("2", "lead"), ("3", "ramp"), ("4", "margin")]:
        both = coord[key] + leaf[key]
        print("  (%s) both boards pooled: n = %d" % (tag, len(both)))

    # Minimum crystal lead, quoted in the text instead of a range.
    print("  crystal ready at least %.3f us before the ramp"
          % min(coord["lead"] + leaf["lead"]))

    # Where the acknowledgment falls inside its window.
    window = leaf["window"]
    if window:
        after_open = statistics.median(window)
        before_close = ACK_WAIT - after_open
        from_centre = (ACK_WAIT / 2.0 - after_open) * 1000.0
        margins = []
        for value in window:
            margins.append(min(value, ACK_WAIT - value))
        print("  ack arrives %.3f us after the window opens and %.3f us before "
              "it closes" % (after_open, before_close))
        print("  that is %.0f ns from the centre, and never within %.3f us of "
              "either edge" % (from_centre, min(margins)))

    print("  keep-alives sent %d, acknowledged on the first attempt %d"
          % (leaf["keep_alives"], leaf["acked"]))


if len(sys.argv) < 2:
    sys.exit("usage: python3 check_e1.py <run folder> [<run folder> ...]")

all_coord = new_samples()
all_leaf = new_samples()

for folder in sys.argv[1:]:
    coord = measure(folder + "/coordinator-logic_analyzer.csv", COORD_PAIR)
    leaf = measure(folder + "/leaf-logic_analyzer.csv", LEAF_PAIR)
    report(folder, coord, leaf)
    for one, total in [(coord, all_coord), (leaf, all_leaf)]:
        for key in one:
            total[key] += one[key]

if len(sys.argv) > 2:
    report("all runs pooled", all_coord, all_leaf)
