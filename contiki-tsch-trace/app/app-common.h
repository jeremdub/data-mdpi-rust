#ifndef APP_COMMON_H_
#define APP_COMMON_H_

#include "contiki.h"
#include "net/linkaddr.h"
#include "net/mac/tsch/tsch.h"

/* All traffic-carrying cells live on a single slotframe. */
#define APP_SLOTFRAME_HANDLE       0

/* Slotframe length, in timeslots. 47 to match Elsts et al., LCN 2016, Sec. VI. */
#define APP_SLOTFRAME_SIZE         47

#define APP_PROBE_SLOTFRAME_HANDLE 2
#define APP_PROBE_SLOT_OFFSET      23
#define APP_PROBE_CHANNEL_OFFSET   0

#define APP_PROBE_LINKADDR_INIT    {{ 0xfe, 0xfe, 0xfe, 0xfe, 0xfe, 0xfe, 0xfe, 0xfe }}

#define APP_SHARED_SLOT_OFFSET     0
#define APP_SHARED_CHANNEL_OFFSET  0

static inline void
app_install_tsch_schedule(void)
{
  struct tsch_slotframe *sf =
    tsch_schedule_add_slotframe(APP_SLOTFRAME_HANDLE, APP_SLOTFRAME_SIZE);

  tsch_schedule_add_link(sf,
      LINK_OPTION_RX | LINK_OPTION_TX | LINK_OPTION_SHARED | LINK_OPTION_TIME_KEEPING,
      LINK_TYPE_ADVERTISING, &tsch_broadcast_address,
      APP_SHARED_SLOT_OFFSET, APP_SHARED_CHANNEL_OFFSET, 1);

#if TSCH_TRACE
  {
    static const linkaddr_t probe_addr = APP_PROBE_LINKADDR_INIT;
    struct tsch_slotframe *probe_sf =
      tsch_schedule_add_slotframe(APP_PROBE_SLOTFRAME_HANDLE, APP_SLOTFRAME_SIZE);

    tsch_schedule_add_link(probe_sf,
        LINK_OPTION_TX,                /* TX only: no RX => never an active slot */
        LINK_TYPE_NORMAL,              /* NORMAL: the EB branch is not taken     */
        &probe_addr,
        APP_PROBE_SLOT_OFFSET, APP_PROBE_CHANNEL_OFFSET, 1);
  }
#endif /* TSCH_TRACE */
}

#endif /* APP_COMMON_H_ */
