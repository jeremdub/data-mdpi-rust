/**
 * \file
 *   Logic-analyzer instrumentation for Contiki-NG TSCH on nRF52840.
 *
 * ---------------------------------------------------------------------------
 * Resource map
 * ---------------------------------------------------------------------------
 *
 *  PPI ch 1  RADIO.EVENTS_FRAMESTART   -> TIMER0.CAPTURE[3]  (owned by driver)
 *            FORK                      -> GPIOTE.TASKS_SET[0] (added here)
 *  PPI ch 2  RADIO.EVENTS_END          -> TIMER0.CAPTURE[2]  (owned by driver)
 *            FORK                      -> GPIOTE.TASKS_CLR[0] (added here)
 *  PPI ch 3  RADIO.EVENTS_RXREADY      -> GPIOTE.TASKS_OUT[1] (toggle)
 *  PPI ch 4  TIMER0.EVENTS_COMPARE[0]  -> GPIOTE.TASKS_OUT[2] (toggle)
 *  PPI ch 5  RADIO.EVENTS_TXREADY      -> GPIOTE.TASKS_OUT[3] (toggle)
 *  PPI ch 6  RADIO.EVENTS_PHYEND       -> GPIOTE.TASKS_OUT[4] (toggle)
 *  PPI ch 7  CLOCK.EVENTS_HFCLKSTARTED -> GPIOTE.TASKS_OUT[5] (toggle)
 *
 *  GPIOTE 0  FRAME    P1.02
 *  GPIOTE 1  RXREADY  P1.06
 *  GPIOTE 2  ENABLE   P1.07
 *  GPIOTE 3  TXREADY  P1.05
 *  GPIOTE 4  PHYEND   P1.01
 *  GPIOTE 5  HFCLK    P1.04
 *  (plain)   SLOT     P1.03
 */

#include "contiki.h"
#include "sys/process.h"
#include "tsch-trace.h"

#include <stdio.h>

#include "hal/nrf_clock.h"
#include "hal/nrf_gpio.h"
#include "hal/nrf_gpiote.h"
#include "hal/nrf_radio.h"
#include "hal/nrf_timer.h"
#include "helpers/nrfx_gppi.h"

/*---------------------------------------------------------------------------*/
#ifndef TRACE_PIN_ENABLE
#define TRACE_PIN_ENABLE  NRF_GPIO_PIN_MAP(1, 7)
#endif
#ifndef TRACE_PIN_RXREADY
#define TRACE_PIN_RXREADY NRF_GPIO_PIN_MAP(1, 6)
#endif
#ifndef TRACE_PIN_TXREADY
#define TRACE_PIN_TXREADY NRF_GPIO_PIN_MAP(1, 5)
#endif
#ifndef TRACE_PIN_HFCLK
#define TRACE_PIN_HFCLK   NRF_GPIO_PIN_MAP(1, 4)
#endif
#ifndef TRACE_PIN_SLOT
#define TRACE_PIN_SLOT    NRF_GPIO_PIN_MAP(1, 3)
#endif
#ifndef TRACE_PIN_FRAME
#define TRACE_PIN_FRAME   NRF_GPIO_PIN_MAP(1, 2)
#endif
#ifndef TRACE_PIN_PHYEND
#define TRACE_PIN_PHYEND  NRF_GPIO_PIN_MAP(1, 1)
#endif

#define GPIOTE_CH_FRAME   0
#define GPIOTE_CH_RXREADY 1
#define GPIOTE_CH_ENABLE  2
#define GPIOTE_CH_TXREADY 3
#define GPIOTE_CH_PHYEND  4
#define GPIOTE_CH_HFCLK   5

#define PPI_CH_FRAMESTART 1
#define PPI_CH_END        2
#define PPI_CH_RXREADY    3
#define PPI_CH_ENABLE     4
#define PPI_CH_TXREADY    5
#define PPI_CH_PHYEND     6
#define PPI_CH_HFCLK      7

#if !defined(NRF_GPIOTE) && defined(NRF_GPIOTE0)
#define NRF_GPIOTE NRF_GPIOTE0
#endif

/*---------------------------------------------------------------------------*/
/* Deferred sync-error log.
 *
 * tsch_slot_operation() runs in the rtimer interrupt. printf() from there
 * would blow the slot budget and can corrupt the UART, which is exactly why
 * Contiki has the TSCH_LOG_ADD ring buffer.
 */
#ifndef TSCH_TRACE_LOG_QUEUE_LEN
#define TSCH_TRACE_LOG_QUEUE_LEN 16 /* must be a power of two */
#endif

struct sync_sample {
  uint32_t asn;
  int32_t err_ns;
  const char *src;
};

static struct sync_sample sync_ring[TSCH_TRACE_LOG_QUEUE_LEN];
static volatile uint8_t sync_head; /* written by the ISR  */
static volatile uint8_t sync_tail; /* written by the process */
static volatile uint16_t sync_dropped;

PROCESS(tsch_trace_process, "TSCH trace log");
/*---------------------------------------------------------------------------*/
void
tsch_trace_log_sync(uint32_t asn_ls4b, const char *src, int32_t err_ns)
{
  uint8_t next = (uint8_t)((sync_head + 1) % TSCH_TRACE_LOG_QUEUE_LEN);

  if(next == sync_tail) {
    sync_dropped++;
    return;
  }

  sync_ring[sync_head].asn = asn_ls4b;
  sync_ring[sync_head].src = src;
  sync_ring[sync_head].err_ns = err_ns;
  sync_head = next;

  process_poll(&tsch_trace_process);
}
/*---------------------------------------------------------------------------*/
PROCESS_THREAD(tsch_trace_process, ev, data)
{
  PROCESS_BEGIN();

  while(1) {
    PROCESS_YIELD_UNTIL(ev == PROCESS_EVENT_POLL);

    while(sync_tail != sync_head) {
      struct sync_sample *s = &sync_ring[sync_tail];
      printf("tsch sync asn=%lu src=%s err_ns=%ld\n",
             (unsigned long)s->asn, s->src, (long)s->err_ns);
      sync_tail = (uint8_t)((sync_tail + 1) % TSCH_TRACE_LOG_QUEUE_LEN);
    }

    if(sync_dropped) {
      printf("tsch sync dropped=%u\n", (unsigned)sync_dropped);
      sync_dropped = 0;
    }
  }

  PROCESS_END();
}
/*---------------------------------------------------------------------------*/
static void
task_pin_setup(uint8_t gpiote_ch, uint32_t pin)
{
  nrf_gpiote_task_configure(NRF_GPIOTE, gpiote_ch, pin,
                            NRF_GPIOTE_POLARITY_TOGGLE,
                            NRF_GPIOTE_INITIAL_VALUE_LOW);
  nrf_gpiote_task_enable(NRF_GPIOTE, gpiote_ch);
}
/*---------------------------------------------------------------------------*/
void
tsch_trace_init(void)
{
  static uint8_t done;

  if(done) {
    return;
  }
  done = 1;

  /* FRAME: RMARKER(+pipeline) .. end of frame, hardware only */
  task_pin_setup(GPIOTE_CH_FRAME, TRACE_PIN_FRAME);

  nrfx_gppi_fork_endpoint_setup(
    PPI_CH_FRAMESTART,
    nrf_gpiote_task_address_get(NRF_GPIOTE,
                                nrf_gpiote_set_task_get(GPIOTE_CH_FRAME)));
  nrfx_gppi_fork_endpoint_setup(
    PPI_CH_END,
    nrf_gpiote_task_address_get(NRF_GPIOTE,
                                nrf_gpiote_clr_task_get(GPIOTE_CH_FRAME)));

  /* PHYEND: last bit on air (TX). The air anchor. */
  task_pin_setup(GPIOTE_CH_PHYEND, TRACE_PIN_PHYEND);

  nrfx_gppi_channel_endpoints_setup(
    PPI_CH_PHYEND,
    nrf_radio_event_address_get(NRF_RADIO, NRF_RADIO_EVENT_PHYEND),
    nrf_gpiote_task_address_get(NRF_GPIOTE,
                                nrf_gpiote_out_task_get(GPIOTE_CH_PHYEND)));

  /*
   * RXREADY / TXREADY: the two radio-ready events
   *
   * Toggle, not SET/CLR, to match the dot15d4 ramp trace: each ready event is
   * one edge, so a second RXREADY inside a slot (transmit() re-enters RX) is
   * visible rather than swallowed by a SET on an already-high pin.
   */
  task_pin_setup(GPIOTE_CH_RXREADY, TRACE_PIN_RXREADY);

  nrfx_gppi_channel_endpoints_setup(
    PPI_CH_RXREADY,
    nrf_radio_event_address_get(NRF_RADIO, NRF_RADIO_EVENT_RXREADY),
    nrf_gpiote_task_address_get(NRF_GPIOTE,
                                nrf_gpiote_out_task_get(GPIOTE_CH_RXREADY)));

  task_pin_setup(GPIOTE_CH_TXREADY, TRACE_PIN_TXREADY);

  nrfx_gppi_channel_endpoints_setup(
    PPI_CH_TXREADY,
    nrf_radio_event_address_get(NRF_RADIO, NRF_RADIO_EVENT_TXREADY),
    nrf_gpiote_task_address_get(NRF_GPIOTE,
                                nrf_gpiote_out_task_get(GPIOTE_CH_TXREADY)));

  /*ENABLE: every rtimer deadline, hardware only */
  task_pin_setup(GPIOTE_CH_ENABLE, TRACE_PIN_ENABLE);

  nrfx_gppi_channel_endpoints_setup(
    PPI_CH_ENABLE,
    nrf_timer_event_address_get(NRF_TIMER0, NRF_TIMER_EVENT_COMPARE0),
    nrf_gpiote_task_address_get(NRF_GPIOTE,
                                nrf_gpiote_out_task_get(GPIOTE_CH_ENABLE)));

   /* HFCLK: crystal ready */
  nrf_gpiote_task_configure(NRF_GPIOTE, GPIOTE_CH_HFCLK, TRACE_PIN_HFCLK,
                            NRF_GPIOTE_POLARITY_TOGGLE,
                            nrf_clock_hf_is_running(NRF_CLOCK,
                                                    NRF_CLOCK_HFCLK_HIGH_ACCURACY)
                            ? NRF_GPIOTE_INITIAL_VALUE_HIGH
                            : NRF_GPIOTE_INITIAL_VALUE_LOW);
  nrf_gpiote_task_enable(NRF_GPIOTE, GPIOTE_CH_HFCLK);

  nrfx_gppi_channel_endpoints_setup(
    PPI_CH_HFCLK,
    nrf_clock_event_address_get(NRF_CLOCK, NRF_CLOCK_EVENT_HFCLKSTARTED),
    nrf_gpiote_task_address_get(NRF_GPIOTE,
                                nrf_gpiote_out_task_get(GPIOTE_CH_HFCLK)));

  nrfx_gppi_channels_enable((1UL << PPI_CH_RXREADY) |
                            (1UL << PPI_CH_ENABLE) |
                            (1UL << PPI_CH_TXREADY) |
                            (1UL << PPI_CH_PHYEND) |
                            (1UL << PPI_CH_HFCLK));

  /* SLOT: plain GPIO, written from the rtimer ISR */
  nrf_gpio_cfg_output(TRACE_PIN_SLOT);
  nrf_gpio_pin_clear(TRACE_PIN_SLOT);

  process_start(&tsch_trace_process, NULL);
}
/*---------------------------------------------------------------------------*/
void
tsch_trace_slot_start(void)
{
  nrf_gpio_pin_set(TRACE_PIN_SLOT);
}
/*---------------------------------------------------------------------------*/
void
tsch_trace_slot_end(void)
{
  nrf_gpio_pin_clear(TRACE_PIN_SLOT);
}
/*---------------------------------------------------------------------------*/
