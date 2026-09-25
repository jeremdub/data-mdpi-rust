/**
 * \file
 *   TSCH coordinator application, link-local only (no RPL / no IPv6 routing).
 *
 *   This node becomes the PAN coordinator: it advertises the network by
 *   sending Enhanced Beacons and acts as the time source the joining nodes
 *   synchronise to. It installs the shared schedule and receives the
 *   link-local frames sent by the joining nodes.
 */

#include "contiki.h"
#include "net/netstack.h"
#include "net/nullnet/nullnet.h"
#include "net/mac/tsch/tsch.h"
#include "app-common.h"
#if TSCH_TRACE
#include "tsch-trace.h"
#endif

#include <string.h>

#include "sys/log.h"
#define LOG_MODULE "Coord"
#define LOG_LEVEL LOG_LEVEL_INFO

/* Periodic liveness print only; the coordinator does not transmit data. */
#define STATUS_INTERVAL (30 * CLOCK_SECOND)

PROCESS(coordinator_process, "TSCH link-local coordinator");
AUTOSTART_PROCESSES(&coordinator_process);

/*---------------------------------------------------------------------------*/
static void
input_callback(const void *data, uint16_t len,
               const linkaddr_t *src, const linkaddr_t *dest)
{
  if(len == sizeof(unsigned)) {
    unsigned count;
    memcpy(&count, data, sizeof(count));
    LOG_INFO("received %u from ", count);
    LOG_INFO_LLADDR(src);
    LOG_INFO_("\n");
  }
}
/*---------------------------------------------------------------------------*/
PROCESS_THREAD(coordinator_process, ev, data)
{
  static struct etimer status_timer;

  PROCESS_BEGIN();

#if TSCH_TRACE
  /* Program the trace pins. Order relative to radio init is irrelevant: the
     PPI forks are independent of the EEP/TEP the radio driver rewrites. */
  tsch_trace_init();
#endif

  /* Become the PAN coordinator (advertiser + time source). */
  tsch_set_coordinator(1);

  /* Install the shared TSCH cell (identical on every node). */
  app_install_tsch_schedule();

  /* Receive link-local frames from the joining nodes. */
  nullnet_set_input_callback(input_callback);

  LOG_INFO("coordinator starting, link addr ");
  LOG_INFO_LLADDR(&linkaddr_node_addr);
  LOG_INFO_("\n");

  etimer_set(&status_timer, STATUS_INTERVAL);
  while(1) {
    PROCESS_WAIT_EVENT_UNTIL(etimer_expired(&status_timer));
    LOG_INFO("associated %u\n", tsch_is_associated);
    etimer_reset(&status_timer);
  }

  PROCESS_END();
}
/*---------------------------------------------------------------------------*/
