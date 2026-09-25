# `timer-toggle`: scheduling a pin every *T* ns, and the errors along the way

This document accompanies the [`timer-toggle`](timer-toggle.rs) example
and the host tool [`tools/timer-decompose.rs`](../../../../../tools/timer-decompose.rs).
It explains how a requested period *T* (in nanoseconds) is realised by the hybrid
RTC + TIMER hardware path the radio driver uses, and quantifies every error term
between the number you ask for and the edge the Saleae sees.

All numbers below were produced and cross-checked with the host tool, which
transcribes the driver's integer math bit-for-bit and asserts the values at
startup.

---

## 1. The two clocks

The driver schedules an instant with a *hybrid* timer:

| Clock | Source | Frequency | Tick |
|-------|--------|-----------|------|
| sleep timer | `RTC2` on the LF crystal (LFXO) | 32 768 Hz | 30517.578125 ns |
| high-precision timer | `TIMER0` on the HF crystal (HFXO) | 16 MHz | 62.5 ns |

A scheduled instant is split into a **coarse RTC part** (an integer number of
30.5 µs RTC ticks) and a **fine TIMER part** (an integer number of 62.5 ns timer
ticks). The RTC compare fires, which *starts* `TIMER0` (over a pre-wired PPI
channel), and `TIMER0` then counts the remainder before toggling the output pin.

```
   |<---------------------- target instant T -------------------------->|
   |<------- coarse: N RTC ticks (30.5 us each) ------->|<-- fine: M --->|
   ^                                                    ^               ^
 anchor                                          RTC epoch          signal
 (now)                                       = TIMER0 start          edge
                                              = ALARM pin           = SIGNAL pin
```

The example exposes both internal boundaries on GPIO so they can be measured
directly (see §5).

---

## 2. The exact integer conversions

The driver never uses floating point. The four conversions (all in
`dot15d4-driver/src/socs/nrf/timer.rs`, struct `TickConversion`) are fixed-point
multiplies followed by a right shift, i.e. they **always floor**:

```
instant_to_rtc_tick(ns)      = (ns   * (2^84 / 5^9)) >> 78
rtc_tick_to_instant(tick)    = (tick * 5^9)          >> 6
duration_to_timer_ticks(ns)  = (ns   * (2^36 / 5^3)) >> 35
timer_ticks_to_duration(tick)= (tick * 5^3)          >> 1
```

Scheduling an instant `at` (from `start_high_precision_timer`) works out to:

```
epoch_rtc_tick = instant_to_rtc_tick(at - TIMER_OFFSET)     // TIMER_OFFSET = 126 ns
epoch_ns       = rtc_tick_to_instant(epoch_rtc_tick)        // the RTC part, <= at
raw            = duration_to_timer_ticks(at - epoch_ns)
cc_value       = raw - 1                                    // -1 pre-compensates a
                                                            //  1-tick PPI start delay
```

The physical signal therefore fires at `epoch_ns + timer_ticks_to_duration(raw)`.

### A note on `duration_to_timer_ticks`

The fixed-point multiplier `2^36 / 5^3` is itself floored, so the function is
**one tick low at exact second boundaries**:

```
duration_to_timer_ticks(1 000 000 000 ns) = 15 999 999   (ideal: 16 000 000)
```

This is harmless here — it is part of the modelled path and is absorbed by the
`raw - 1` / `+1 PPI tick` bookkeeping — but it is worth knowing if you compare
the firmware against a naive `ns / 62.5` calculation.

---

## 3. Quantization error (integer path only)

Because every conversion floors, the realised edge is **always at or before** the
requested instant, never after, and the shortfall is bounded by roughly one timer
tick.

Sweeping all 65 536 consecutive ns targets in a one-second neighbourhood:

```
per-edge quantization error  ∈  [ -63 , -1 ] ns  =  [ -1.008 , -0.016 ] timer ticks
```

So the integer decomposition costs **at most ~1 timer tick (62.5 ns), one-sided**.
This matches the driver's stated design target of ±1 timer-tick scheduling error.

### Worked example, *T* = 1 s (anchor at *t* = 5 s, first edge at 6 s)

```
target uptime          = 6 000 000 000 ns
RTC epoch tick         = 196607  ->  epoch_ns = 5 999 969 482 ns
coarse RTC part        = 30 518 ns before target  (1.00 RTC tick)
TIMER part (cc_value)  = 487 ticks  ->  alarm->signal gap = 30 500 ns
modeled signal fire    = 5 999 999 982 ns
quantization error     = -18 ns   (-0.288 timer ticks)
```

---

## 4. Jitter and drift across many periods

The firmware keeps the target in the **exact ns domain** (`timeout += PERIOD`)
and re-quantizes independently each period. It does **not** re-read `now()` or
accumulate ticks. Two consequences:

1. **No unbounded drift, ever.** Each period's error is re-derived against the
   exact target, so quantization errors do not pile up. (Crystal error still
   accumulates as phase — that is physics, not the integer path; see §6.)

2. **Interval jitter depends on whether *T* lands on the sub-tick grid.**

| *T* | per-edge error | peak-to-peak interval jitter |
|-----|----------------|------------------------------|
| 1 s (32768 RTC ticks **exactly**, 16 M timer ticks exactly) | constant −18 ns | **0 ns** |
| 0.5 s (exact) | constant | 0 ns |
| 333 333 333 ns | [−47, −8] ns | 45 ns |
| 100 000 000 ns | [−61, −18] ns | 62 ns |
| 7 000 000 ns | [−63, −1] ns | 81 ns |

For *T* = 1 s the sub-tick phase repeats every period (1 s is an integer number of
*both* tick periods), so you get a constant −18 ns DC offset and a perfectly
periodic square wave. Pick a "clean" *T* like 1 s or 0.5 s if you want the
quantization to show up as a fixed offset rather than dither.

---

## 5. What the logic analyzer pins isolate

The example drives three output pins, all toggled in hardware (PPI → GPIOTE, no
CPU in the loop):

| Pin | nRF pin | Toggles at | Reveals |
|-----|---------|-----------|---------|
| `PIN_TIMER_TICK`   | **P0.31** | every RTC tick (free-running) | the **actual LFXO frequency** (30517.578 ns nominal) |
| `PIN_TIMER_ALARM`  | **P0.30** | RTC epoch = `TIMER0` start | the RTC/TIMER **boundary** (new in this example) |
| `PIN_TIMER_SIGNAL` | **P0.29** | the scheduled target instant | the realised edge |

(`PIN_TIMER_EVENT` P0.02 is an input, used only by the device-sync examples.)

Three measurements, three different things:

- **`tick` period** → LFXO frequency error (the coarse clock). Drifts ±20–40 ppm.
- **`alarm` → `signal` gap** → the **TIMER-resolved remainder** of the
  decomposition. This is driven entirely by the 16 MHz HFXO and `TIMER0`, so it is
  **immune to LFXO drift**. *This is the gold measurement to verify the
  decomposition*: it should equal `timer_ticks_to_duration(raw)` from the tool
  (e.g. 30 500 ns for *T* = 1 s) to within ±1 timer tick of clock-domain jitter.
- **`signal` → `signal` gap** → the measured period = *T* · (1 + crystal error).

The new `PIN_TIMER_ALARM` is what makes the second measurement possible. It is
generated by routing the **unused fork** of the static
`RTC_CC0 → TIMER0.tasks_start` PPI channel to a GPIOTE toggle task, so the pin
flips on the very same compare event that starts `TIMER0` — no extra PPI hop, no
added latency. It only fires on the explicit-`at` scheduling path (which this
example always uses); the event-capture path that starts the timer from
`events_tick` does not toggle it.

---

## 6. Physical error sources (independent of the integer math)

These dominate over the ≤62.5 ns quantization for any reasonably large *T*:

- **Crystal frequency error scales with *T*.** At *T* = 1 s:

  | LFXO/HFXO error | period error contribution |
  |-----------------|---------------------------|
  | ±10 ppm | ±10 µs |
  | ±20 ppm | ±20 µs |
  | ±40 ppm | ±40 µs |

  Quantization at *T* = 1 s is ~0.0625 ppm — i.e. **~300–600× smaller** than
  crystal error. LFXO error shifts the epoch (visible on the `tick` pin); HFXO
  error sets the 16 MHz rate (visible in the `alarm → signal` gap).

  > To *see* the 62.5 ns quantization rather than crystal drift, measure the
  > `alarm → signal` gap (HFXO-only), not the full period.

- **RTC → TIMER clock-domain crossing.** Starting a 16 MHz timer from a 32.768 kHz
  compare event is an async crossing worth **±1 timer tick (62.5 ns)** of
  hardware jitter on the alarm/epoch edge.

- **GPIOTE / PPI propagation.** Fixed sub-tick latency on each pin; it is common
  to all edges so it cancels in *period* measurements and adds a small constant
  to the *alarm → signal* gap.

If you need to remove the LFXO contribution from the headline period number,
the driver ships a skew-measurement example,
[`dot15d4-driver/examples/nrf/timer/measure-rtc-skew.rs`](../../../../../dot15d4-driver/examples/nrf/timer/measure-rtc-skew.rs),
which measures the LFXO frequency against the HFXO.

---

## 7. Running it

### Predict the numbers on the host (no hardware)

```sh
rustc -O tools/timer-decompose.rs -o /tmp/timer-decompose
/tmp/timer-decompose 1000000000      # any period in ns
```

It prints the representability of *T*, the first-edge decomposition, a 32-period
steady-state jitter/drift scan, the crystal-error budget, and the pin mapping.

### Flash and measure

Set the period at the top of [`timer-toggle.rs`](timer-toggle.rs):

```rust
const PERIOD: NsDuration = NsDuration::secs(1);
```

Then:

```sh
# RTT log (decomposition per period) needs the defmt backend:
cargo embed --release --bin timer-toggle --features timer-trace,defmt flash
cargo embed --release --bin timer-toggle --features timer-trace,defmt rtt
```

Without `defmt`/`log` the `info!` lines compile to no-ops and you simply get the
GPIO trace. The example logs, each period, the same `rtc_tick / epoch / timer_ticks
/ gap / err` fields the host tool prints, so you can line the RTT log up against
the analyzer capture.

> The pin must *toggle* each period, so a full square-wave period on the analyzer
> is `2 · PERIOD`; the edge-to-edge spacing is `PERIOD`.

---

## 8. Summary

- The ns → (RTC tick + TIMER tick) decomposition is exact integer arithmetic that
  **always floors**, giving a one-sided quantization error of **≤ 1 timer tick
  (62.5 ns)** per edge.
- It **never drifts**: the firmware re-quantizes the exact target each period.
- For *T* = 1 s the error is a **constant −18 ns** with **zero interval jitter**,
  because 1 s is an exact integer of both tick periods.
- Real-world period accuracy is set by **crystal error (±10–40 ppm → ±10–40 µs/s)**,
  which swamps quantization. Measure the **`alarm → signal` gap** to observe the
  62.5 ns quantization in isolation, since that interval is HFXO-only.
