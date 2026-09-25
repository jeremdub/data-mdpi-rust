#ifndef PROJECT_CONF_H_
#define PROJECT_CONF_H_

/* Optional TSCH link-layer security (off by default). */
#ifndef WITH_SECURITY
#define WITH_SECURITY 0
#endif
#if WITH_SECURITY
#define LLSEC802154_CONF_ENABLED 1
#endif

/*******************************************************/
/********************* TSCH schedule *******************/
/*******************************************************/

/* We install our own schedule (see app-common.h); disable the built-in
   6TiSCH minimal schedule so it does not add a competing cell at (0,0). */
#define TSCH_SCHEDULE_CONF_WITH_6TISCH_MINIMAL 0

#define TSCH_PACKET_CONF_EB_WITH_SLOTFRAME_AND_LINK 1

/*******************************************************/
/******************* Channel / PAN *********************/
/*******************************************************/
#define TSCH_CONF_DEFAULT_HOPPING_SEQUENCE (uint8_t[]){ 26 }
#define TSCH_CONF_JOIN_HOPPING_SEQUENCE    (uint8_t[]){ 26 }

#define IEEE802154_CONF_PANID 0xabcd

/*******************************************************/
/****************** Enhanced Beacons *******************/
/*******************************************************/

#define TSCH_CONF_EB_PERIOD     (10 * CLOCK_SECOND)
#define TSCH_CONF_MAX_EB_PERIOD (10 * CLOCK_SECOND)

#undef TSCH_SCHEDULE_CONF_DEFAULT_LENGTH
#define TSCH_SCHEDULE_CONF_DEFAULT_LENGTH  101
#define TSCH_CONF_ADAPTIVE_TIMESYNC        1
#define TSCH_CONF_BASE_DRIFT_PPM           0

#define TSCH_CONF_KEEPALIVE_TIMEOUT        (120 * CLOCK_SECOND)
#define TSCH_CONF_MAX_KEEPALIVE_TIMEOUT    (120 * CLOCK_SECOND)
#define TSCH_CONF_DESYNC_THRESHOLD         (240 * CLOCK_SECOND)

#define TSCH_CONF_TIMESYNC_REMOVE_JITTER   0

/*******************************************************/
/***************** Logic-analyzer trace ****************/
/*******************************************************/

/*
 * Slot-start / slot-end markers. Declared here rather than including
 * tsch-trace.h: project-conf.h is pulled in very early by contiki-conf.h and
 * must stay free of nrfx headers.
 */
#ifndef __ASSEMBLER__
void tsch_trace_slot_start(void);
void tsch_trace_slot_end(void);
#endif
#define TSCH_DEBUG_SLOT_START() tsch_trace_slot_start()
#define TSCH_DEBUG_SLOT_END()   tsch_trace_slot_end()

/*******************************************************/
/*********************** Logging ***********************/
/*******************************************************/

#define LOG_CONF_LEVEL_MAC     LOG_LEVEL_DBG
#define LOG_CONF_LEVEL_FRAMER  LOG_LEVEL_NONE
#define TSCH_LOG_CONF_PER_SLOT 0

#endif /* PROJECT_CONF_H_ */
