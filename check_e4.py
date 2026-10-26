#!/usr/bin/env python3
"""E4: Table 9 and Supplementary Table S10, from the logs of the three variants.

    python3 check_e4.py            # runs 1 to 10 of each variant
    python3 check_e4.py 1 2 3      # some runs

Both stacks print the same synchronization line, so one parser reads every run:

    [ <t>s] <node> | tsch sync asn=<n> src=frame|ack|peer err_ns=<e>

Counts cover the whole run; errors and corrections follow the 600 s warm-up.
"""
import math
import os
import re
import statistics
import sys

WARMUP = 600.0
DATA = "e4-interoperability/data"
VARIANTS = [("B-pre", "coord-contiki_leaf-rust-before-patch"),
            ("B-post", "coord-contiki_leaf-rust"),
            ("A", "coord-rust_leaf-contiki")]
T975 = {1: 12.7062, 2: 4.3027, 3: 3.1824, 4: 2.7764, 5: 2.5706, 6: 2.4469, 7: 2.3646,
        8: 2.3060, 9: 2.2622, 10: 2.2281, 11: 2.2010, 12: 2.1788, 13: 2.1604, 14: 2.1448}
AT = re.compile(r"\[\s*([0-9.]+)s\]\s+(\S+)\s+\|")
SYNC = re.compile(r"tsch sync asn=(\d+) src=(\w+) err_ns=(-?\d+)")
SENT = re.compile(r"tsch tx sent asn=\d+ shared=\w+ attempts=(\d+)")
KEEPALIVE = re.compile(r"tsch keep-alive sent asn=(\d+)")
CONFIRM = re.compile(r"val tx seq=\d+ ok=(\d)")
JOINED = re.compile(r"val join ok .*ms=(\d+)")


def read(path):
    join = associated = boot = None
    beacons = requested = delivered = 0
    events, keepalives, attempts = [], set(), []
    for line in open(path, encoding="utf-8", errors="replace"):
        at = AT.search(line)
        if at is None:
            continue
        t, node = float(at.group(1)), at.group(2)
        if node == "root":
            if associated is not None and ("tsch beacon sent" in line or "packet sent" in line):
                beacons += 1
            continue
        if node != "leaf":
            continue
        if boot is None:
            boot = t
        if join is None and ("val join ok" in line or "association done" in line):
            stamped = JOINED.search(line)
            join = int(stamped.group(1)) / 1000.0 if stamped else t - boot
            associated = t
        confirm = CONFIRM.search(line)
        if confirm:
            requested += 1
            delivered += confirm.group(1) == "1"
        elif "[INFO: Node      ] sending" in line:
            requested += 1
        elif "[INFO: TSCH      ] packet sent" in line:
            delivered += 1
        m = SENT.search(line)
        if m:
            attempts.append(int(m.group(1)))
        m = KEEPALIVE.search(line)
        if m:
            keepalives.add(int(m.group(1)))
        m = SYNC.search(line)
        if m:
            events.append((t, int(m.group(1)), m.group(2), int(m.group(3)) / 1000.0))
    if join is None:
        sys.exit("%s: no association line; is this an E4 log?" % path)
    return {"join": join, "beacons": beacons, "events": events, "keepalives": keepalives,
            "attempts": attempts, "requested": requested, "delivered": delivered}


def run(path):
    r = read(path)
    frames, acked, corr, errs = 0, 0, [], []
    for t, asn, src, err in r["events"]:
        if src == "frame":
            frames += 1
            if t > WARMUP:
                errs.append(err)
        elif src == "ack":
            acked += asn not in r["keepalives"]
            if t > WARMUP:
                corr.append(err)
    return {"join": r["join"], "eb_rx": frames, "eb_tx": r["beacons"], "req": r["requested"],
            "dlv": r["delivered"], "acked": acked,
            "retried": sum(1 for c in r["attempts"] if c > 1), "corr": corr, "errs": errs}


def load(root=".", runs=range(1, 11)):
    return {label: [run(os.path.join(root, DATA, folder, "run%d" % i, "log.txt")) for i in runs]
            for label, folder in VARIANTS}


def pool(xs):
    errs = [e for x in xs for e in x["errs"]]
    corr = [c for x in xs for c in x["corr"]]
    out = {k: sum(x[k] for x in xs) for k in ("req", "dlv", "acked", "retried", "eb_rx", "eb_tx")}
    out.update(joins=[x["join"] for x in xs], n=len(errs), nc=len(corr),
               mae=statistics.fmean(abs(e) for e in errs), sd=statistics.pstdev(errs),
               tc=statistics.fmean(corr), far=sorted(e for e in errs if abs(e) > 1.0),
               mae_runs=[statistics.fmean(abs(e) for e in x["errs"]) for x in xs],
               tc_runs=[statistics.fmean(x["corr"]) for x in xs])
    return out


def ci95(values):
    m = statistics.fmean(values)
    h = T975[len(values) - 1] * statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return m - h, m + h


if __name__ == "__main__":
    runs = [int(a) for a in sys.argv[1:]] or list(range(1, 11))
    data = load(".", runs)
    print("%-7s %4s %6s %6s %6s %7s %6s %10s %8s %8s %5s" % (
        "variant", "run", "join", "EB RR", "PDR", "Enh-ACK", "Retr", "TC IE (us)", "MAE (us)", "sd (us)", "n"))
    for label, _ in VARIANTS:
        for i, x in zip(runs, data[label]):
            print("%-7s %4d %6.1f %6.1f %6.1f %7.1f %6.1f %+10.3f %8.3f %8.3f %5d" % (
                label, i, x["join"], 100.0 * x["eb_rx"] / x["eb_tx"], 100.0 * x["dlv"] / x["req"],
                100.0 * x["acked"] / x["dlv"], 100.0 * x["retried"] / x["req"],
                statistics.fmean(x["corr"]), statistics.fmean(abs(e) for e in x["errs"]),
                statistics.pstdev(x["errs"]), len(x["errs"])))
    print()
    for label, _ in VARIANTS:
        f = pool(data[label])
        print("%-7s join %.1f-%.1f s, EB RR %.1f%% (%d/%d), PDR %.1f%%, Enh-ACK %.1f%%, Retr %.1f%% (%d/%d)" % (
            label, min(f["joins"]), max(f["joins"]), 100.0 * f["eb_rx"] / f["eb_tx"], f["eb_rx"], f["eb_tx"],
            100.0 * f["dlv"] / f["req"], 100.0 * f["acked"] / f["dlv"],
            100.0 * f["retried"] / f["req"], f["retried"], f["req"]))
        print("        TC IE %+.3f us over %d (CI %+.3f to %+.3f), MAE %.3f us over %d (CI %.3f to %.3f), "
              "sd %.3f us, %d beyond 1 us" % ((f["tc"], f["nc"]) + ci95(f["tc_runs"]) + (f["mae"], f["n"])
                                             + ci95(f["mae_runs"]) + (f["sd"], len(f["far"]))))
