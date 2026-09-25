CONTIKI_PROJECT = coordinator joining-node
all: $(CONTIKI_PROJECT)

# CONTIKI ?= ../../..

MAKE_MAC = MAKE_MAC_TSCH
MAKE_NET = MAKE_NET_NULLNET

MAKE_ROUTING = MAKE_ROUTING_NULLROUTING

MAKE_WITH_SECURITY ?= 0
ifeq ($(MAKE_WITH_SECURITY),1)
CFLAGS += -DWITH_SECURITY=1
endif

SENDER ?= 0
ifeq ($(SENDER),1)
CFLAGS += -DSEND_DATA=1
endif

# rtimer tick, in Hz. Needs patches/0004, which makes the rate configurable.
#   1000000  one tick per microsecond  (E3c, E4)   -- the default here
#     62500  one tick per 16 us        (E3b)       -- the rate the nRF port ships
RTIMER_SECOND ?= 1000000
CFLAGS += -DRTIMER_ARCH_SECOND=$(RTIMER_SECOND)

# Logic-analyzer instrumentation (TARGET=nrf BOARD=nrf52840/dk only).
# Build without it with: make TRACE=0
TRACE ?= 1
ifeq ($(TRACE),1)
PROJECT_SOURCEFILES += tsch-trace.c
CFLAGS += -DTSCH_TRACE=1

CFLAGS += -DNRFX_GPIOTE0_CHANNELS_USED=0x3F
endif

include $(CONTIKI)/Makefile.include
