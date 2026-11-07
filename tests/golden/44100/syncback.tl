timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch syncback 0 65536 0 0 0 0 2 100 0 120
patch syncback 1 32768 0 0 0 0 2 100 0 120
patch syncback 2 180224 120 0 0 0 2 100 0 120
route syncback 3 4 0>2:256:sync 1>2:128:ring
voice 0 syncback
hash 93a07147ed86d07c
0 golden 0 on 220000 32767
1100000000 golden 0 off
