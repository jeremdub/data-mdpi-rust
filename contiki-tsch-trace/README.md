# Contiki-NG TSCH tracing (nRF52840-DK)

The Contiki-NG side of the bench: the baseline of E3b and E3c, and the
interoperability peer of E4. It emits the same serial synchronization line and
the same hardware-timed pin edges as dot15d4, so one harness drives both
stacks.

    app/        Contiki-NG project: coordinator and joining-node
    patches/    0001  the tsch sync-error log line

## Build

    ./setup-contiki-ng.sh ~/contiki-ng          # clone, pin to 7f844118d, patch
    cd app
    make TARGET=nrf BOARD=nrf52840/dk CONTIKI=~/contiki-ng -j8

    make ... RTIMER_SECOND=62500                # E3b, the tick the port ships
    make ... RTIMER_SECOND=1000000              # E3c and E4 (the default)

Toolchain: ARM GNU / xPack `arm-none-eabi-gcc` (validated: 12.2.1). `srec_cat`
(package `srecord`) is needed for `.hex` output. `TRACE=0` strips every trace
pin; `MAKE_WITH_SECURITY=1` enables TSCH link-layer security.

## Flash

    make TARGET=nrf BOARD=nrf52840/dk CONTIKI=~/contiki-ng \
         coordinator.upload NRF_UPLOAD_SN=<jlink-serial>

`joining-node` likewise, the same binary on every leaf: the address and the
time source are learned at run time. Needs `nrfutil` with the `device` command.

## Output

Serial, UARTE0, 115200 8N1 on the DK's VCOM (TX = P0.06), not RTT/defmt:

    tsch sync asn=<u32> src=frame|ack err_ns=<i32>

`src=frame` is predicted minus measured on the slot-start reference, the same
definition dot15d4 logs; `src=ack` is the Time Correction IE received. Both are
quantized to one rtimer tick. An ISR-safe ring buffer carries the line out of
the slot operation, printed from a polled process.

`check_e3.py` reads FRAME on the coordinator and SLOT and RT on the leaf.
