/**
 * \file
 *   Logic-analyzer instrumentation for Contiki-NG TSCH on nRF52840
 *   (TARGET=nrf BOARD=nrf52840/dk).
 *
 *   Seven pins, all on GPIO port 1 (port 0 is taken by the DK LEDs P0.13-16,
 *   the buttons P0.11/12/24/25 and UARTE0 P0.06/P0.08). The channel order is
 *   pin-for-pin the one dot15d4's `tsch-ramp-trace` feature uses, so the same
 *   probe harness and the same analyser project drive both stacks without
 *   rewiring:
 *
 *     P1.07  ENABLE  hardware  toggle on TIMER0 COMPARE[0]
 *     P1.06  RXREADY hardware  toggle on RADIO.EVENTS_RXREADY
 *     P1.05  TXREADY hardware  toggle on RADIO.EVENTS_TXREADY
 *     P1.04  HFCLK   hardware  toggle on CLOCK.EVENTS_HFCLKSTARTED
 *     P1.03  SLOT    software  high for the duration of a TSCH slot operation
 *     P1.02  FRAME   hardware  rise = RADIO.EVENTS_FRAMESTART
 *                              fall = RADIO.EVENTS_END
 *     P1.01  PHYEND  hardware  toggle on RADIO.EVENTS_PHYEND
 *
 *   Every pin except SLOT carries no CPU jitter at all: radio/timer/clock
 *   event -> PPI -> GPIOTE task. SLOT is a plain GPIO write from the rtimer
 *   ISR and is a segmentation marker, never a timing reference.
 *
 */

#ifndef TSCH_TRACE_H_
#define TSCH_TRACE_H_

#include <stdint.h>

/**
 * Program the GPIOTE channels and the PPI forks/channels. Idempotent; safe to
 * call before or after NETSTACK_RADIO.init().
 */
void tsch_trace_init(void);

/** Rising edge of the SLOT pin. Wired to TSCH_DEBUG_SLOT_START(). */
void tsch_trace_slot_start(void);

/** Falling edge of the SLOT pin. Wired to TSCH_DEBUG_SLOT_END(). */
void tsch_trace_slot_end(void);

/**
 * Queue one synchronisation-error sample for printing from process context.
 *
 * Callable from the rtimer ISR: the sample is pushed to a ring buffer and the
 * printing process is polled. \p src is a static string ("frame" or "ack").
 * The emitted line matches dot15d4's sync-error-log format, so the same
 * SyncErrorListener regex in experiment.py parses both stacks:
 *
 *   tsch sync asn=<u32> src=frame|ack err_ns=<i32>
 */
void tsch_trace_log_sync(uint32_t asn_ls4b, const char *src, int32_t err_ns);

#endif /* TSCH_TRACE_H_ */
