# The segment clamp's edge  at 4,096,000 Hz: a 32,767 ms decay or
# release is 134,213,632 samples, below FM_SEG_MAX (2^27 - 1), the largest
# count fm_rate shifts unclamped; 32,768 ms is 2^27, clamped to it, and so
# is 65,535 ms. Three full-scale carriers: the mix saturates by design.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 4096000
length_ns 2000000
streams edge
patch below 0 65536 0 0 0 256 0 32767 1200 32767
patch below 1 131072 0 2048 512 0 32767 32767 600 32767
patch at 0 65536 0 0 0 256 0 32768 1200 32768
patch at 1 131072 0 2048 512 0 32768 32768 600 32768
patch top 0 65536 0 0 0 256 65535 65535 1200 65535
patch top 1 65536 0 1024 1024 0 65535 65535 1200 65535
voice 0 below
voice 1 at
voice 2 top
hash 5bedbb2e23869941
0 edge 0 on 440000 32767
0 edge 1 on 440000 32767
0 edge 2 on 440000 32767
500000 edge 0 pitch 4294967295
1000000 edge 0 off
1000000 edge 1 off
1200000 edge 0 on 880000 32767
1500000 edge 2 off
1999999 edge 1 on 220000 16384
