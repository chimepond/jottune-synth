timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch ringedge 0 65536 120 0 0 0 2 100 0 120
patch ringedge 1 98304 0 0 0 0 2 100 0 120
patch ringedge 2 16384 0 0 0 0 2 100 0 120
route ringedge 3 1 1>0:256:ring 2>0:128:am
voice 0 ringedge
hash 2f9ff5b6a1a9a232
0 golden 0 on 220000 32767
1100000000 golden 0 off
