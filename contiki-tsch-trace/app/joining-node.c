/**
 * \file
 *   TSCH joining-node application, link-local only (no RPL / no IPv6 routing).
 *
 *   This node is NOT a coordinator. It scans the channels, hears the
 *   coordinator's Enhanced Beacons, associates (joins) the TSCH network,
 *   installs the same shared schedule, and then sends one link-local frame
 *   to the coordinator every SEND_INTERVAL.
 */

#include "contiki.h"
#include "net/netstack.h"
#include "net/nullnet/nullnet.h"
#include "net/mac/tsch/tsch.h"
#include "net/mac/tsch/tsch-queue.h"
#include "app-common.h"
#if TSCH_TRACE
#include "tsch-trace.h"
#endif

#include <string.h>

#include "sys/log.h"
#define LOG_MODULE "Node"
#define LOG_LEVEL LOG_LEVEL_INFO

/* How often the joining node sends one link-local frame to the coordinator. */
#define SEND_INTERVAL (15 * CLOCK_SECOND)

/*
 * Learned at runtime: the TSCH time source is, by construction, the node
 * whose EB we associated to, i.e. the coordinator in this 1-hop bench.
 */
static linkaddr_t coordinator_addr;

PROCESS(joining_node_process, "TSCH link-local joining node");
AUTOSTART_PROCESSES(&joining_node_process);

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
PROCESS_THREAD(joining_node_process, ev, data)
{
  static struct etimer timer;
  static unsigned count = 0;

  PROCESS_BEGIN();

#if TSCH_TRACE
  tsch_trace_init();
#endif

  /* Install the same shared TSCH cell as the coordinator. */
  app_install_tsch_schedule();

  nullnet_set_input_callback(input_callback);
  nullnet_buf = (uint8_t *)&count;
  nullnet_len = sizeof(count);

  /* Wait until associated and the time source (== coordinator) is known. */
  etimer_set(&timer, CLOCK_SECOND);
  while(!tsch_is_associated || tsch_queue_get_time_source() == NULL) {
    PROCESS_WAIT_UNTIL(etimer_expired(&timer));
    etimer_reset(&timer);
  }
  linkaddr_copy(&coordinator_addr,
                tsch_queue_get_nbr_address(tsch_queue_get_time_source()));
  /*
   * Leaves stay silent on EBs, like the dot15d4 bench (BEACON=0 on leaves).
   * With NullRouting is_in_leaf_mode() == 0, so leaves would otherwise
   * advertise too and contend with the coordinator on slot 0.
   */
  tsch_set_eb_period(0);
  LOG_INFO("associated, start sending to ");
  LOG_INFO_LLADDR(&coordinator_addr);
  LOG_INFO_("\n");

  etimer_set(&timer, SEND_INTERVAL);
  while(1) {
    PROCESS_WAIT_EVENT_UNTIL(etimer_expired(&timer));
#ifdef SEND_DATA
    if(tsch_is_associated && tsch_queue_get_time_source() != NULL) {
      linkaddr_copy(&coordinator_addr,
                    tsch_queue_get_nbr_address(tsch_queue_get_time_source()));
      tsch_set_eb_period(0);

      LOG_INFO("sending %u to ", count);
      LOG_INFO_LLADDR(&coordinator_addr);
      LOG_INFO_("\n");

      nullnet_len = sizeof(count);
      NETSTACK_NETWORK.output(&coordinator_addr);
      count++;
    }
#endif

    etimer_reset(&timer);
  }

  PROCESS_END();
}
/*---------------------------------------------------------------------------*/
