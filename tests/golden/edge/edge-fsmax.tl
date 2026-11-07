# Edge inputs of  at the highest rate, 4,294,967,295 Hz: 4,294 samples,
# every segment of 32 ms or more clamped to FM_SEG_MAX. Too fast for a WAV
# file: --wav must refuse it (check.sh). The mix saturates by design.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 4294967295
length_ns 1000
streams edge
patch long 0 65536 0 0 0 256 0 65535 0 65535
patch long 1 65536 0 2048 512 0 65535 65535 600 65535
patch max 0 4294967295 65535 65535 65535 65535 65535 65535 65535 65535 4294967295 65535
patch max 1 4294967295 65535 65535 65535 65535 65535 65535 65535 65535 4294967295 65535
voice 0 long
voice 1 max
hash 49fcf0868517d57d
0 edge 0 on 440000 32767
0 edge 1 on 4294967295 65535
100 edge 0 pitch 4294967295
300 edge 0 off
400 edge 0 on 880000 32767
500 edge 0 pitch 0
999 edge 0 off
