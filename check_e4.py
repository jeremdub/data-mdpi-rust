#!/usr/bin/env python3
"""E4: Table 8, from the raw logs of the three interoperability runs.

    python3 check_e4.py e4-interoperability/data/*-log.txt

One harness drives both stacks, so both print the same synchronization line
and one parser reads all three runs:

    [ <t>s] <node> | tsch sync asn=<n> src=frame|ack|peer err_ns=<e>

The joined node is the one that is not the coordinator. Counts are over the
whole run, the two error columns are over the events that follow the 600 s
warm-up
"""

import re
import statistics
import sys

WARMUP = 600.0      # s discarded at the start of a run, as everywhere else

AT = re.compile(r"\[\s*([0-9.]+)s\]\s+(\S+)\s+\|")
SYNC = re.compile(r"tsch sync asn=(\d+) src=(\w+) err_ns=(-?\d+)")
SENT = re.compile(r"tsch tx sent asn=\d+ shared=\w+ attempts=(\d+)")
KEEPALIVE = re.compile(r"tsch keep-alive sent asn=(\d+)")
CONFIRM = re.compile(r"val tx seq=\d+ ok=(\d)")
JOINED = re.compile(r"val join ok .*ms=(\d+)")


def read(path):
    join = None            # the joined node's uptime at association, in s
    associated = None      # when that happened, in run time
    boot = None
    beacons = 0
    events = []            # (time, asn, src, error in us)
    keepalives = set()
    attempts = []
    requested = 0
    delivered = 0

    for line in open(path, encoding="utf-8", errors="replace"):
        at = AT.search(line)
        if at is None:
            continue
        time = float(at.group(1))
        node = at.group(2)

        # The coordinator, whichever stack it runs, reports every beacon it sends.
        if node == "root":
            if "tsch beacon sent" in line or "packet sent" in line:
                if associated is not None:
                    beacons += 1
            continue
        if node != "leaf":
            continue
        if boot is None:
            boot = time

        # Association: "val join ok" on dot15d4, "association done" on Contiki.
        # dot15d4 stamps its own uptime; a Contiki leaf does not, so take the
        # time since its first line. Either way the column is boot to assoc.
        if join is None and ("val join ok" in line or "association done" in line):
            stamped = JOINED.search(line)
            if stamped:
                join = int(stamped.group(1)) / 1000.0
            else:
                join = time - boot
            associated = time

        # Data frames. A dot15d4 leaf confirms each one; a Contiki leaf does
        # not, so its requests and its deliveries are two different log lines.
        confirm = CONFIRM.search(line)
        if confirm:
            requested += 1
            if confirm.group(1) == "1":
                delivered += 1
        elif "[INFO: Node      ] sending" in line:
            requested += 1
        elif "[INFO: TSCH      ] packet sent" in line:
            delivered += 1

        sent = SENT.search(line)
        if sent:
            attempts.append(int(sent.group(1)))

        keepalive = KEEPALIVE.search(line)
        if keepalive:
            keepalives.add(int(keepalive.group(1)))

        sync = SYNC.search(line)
        if sync:
            events.append((time, int(sync.group(1)), sync.group(2),
                           int(sync.group(3)) / 1000.0))

    if join is None:
        sys.exit("%s: no association line; is this an E4 log?" % path)

    return {"join": join, "beacons": beacons, "events": events,
            "keepalives": keepalives, "attempts": attempts,
            "requested": requested, "delivered": delivered}


def row(path):
    run = read(path)

    frames = []            # every beacon the joined node synchronized on
    corrections = []       # Time Correction IEs, after the warm-up
    errors = []            # frame-based errors, after the warm-up
    acked = 0
    for time, asn, src, error in run["events"]:
        if src == "frame":
            frames.append(error)
            if time > WARMUP:
                errors.append(error)
        elif src == "ack":
            if asn not in run["keepalives"]:
                acked += 1
            if time > WARMUP:
                corrections.append(error)

    retried = 0
    for count in run["attempts"]:
        if count > 1:
            retried += 1

    absolute = []
    for error in errors:
        absolute.append(abs(error))

    name = path.split("/")[-1].replace("interop-aug30_", "")[:40]
    print("%-40s %5.1f %5d/%-5d %6.1f %5d %6.1f %7.1f %6.1f %+8.3f/%-4d %7.3f/%-4d"
          % (name, run["join"], len(frames), run["beacons"],
             100.0 * len(frames) / run["beacons"],
             run["requested"], 100.0 * run["delivered"] / run["requested"],
             100.0 * acked / run["delivered"],
             100.0 * retried / run["requested"],
             statistics.fmean(corrections), len(corrections),
             statistics.fmean(absolute), len(absolute)))

    # A reference error the joined node oscillates over shows up as two modes:
    # every ack moves its clock, the next beacon moves it back (Section 5.4).
    far = []
    for error in errors:
        if abs(error) > 1.0:
            far.append(error)
    if far:
        far.sort()
        print("    two modes: %d of %d errors beyond 1 us (median %+.3f us), "
              "%d below" % (len(far), len(errors), far[len(far) // 2],
                            len(errors) - len(far)))


if len(sys.argv) < 2:
    sys.exit(__doc__)

print("%-40s %5s %11s %6s %5s %6s %7s %6s %13s %12s"
      % ("run", "join", "EB rx/tx", "EB RR", "data", "PDR", "Enh-ACK", "Retr",
         "TC IE (us)", "MAE (us)"))
for path in sys.argv[1:]:
    row(path)
