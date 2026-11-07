timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch modsmooth 0 65536 120 0 0 0 2 100 0 180
patch modsmooth 1 131072 0 1024 0 0 2 100 0 180
mod modsmooth 0 ctl0 level0 120 3
voice 0 modsmooth
hash 73343e6613b72177
0 golden 0 on 220000 32767
100000000 golden 0 ctl 0 32768
400000000 golden 0 ctl 0 -32768
700000000 golden 0 on 220000 24000
1100000000 golden 0 off
