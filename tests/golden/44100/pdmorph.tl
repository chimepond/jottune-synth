timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
streams golden
patch pdmorph 0 65536 180 0 0 0 5 100 0 300
patch pdmorph 1 65536 0 0 0 0 5 100 0 300
peg pdmorph 0 200 0 0 0 0 -1200 200 3
mod pdmorph 0 ctl0 pd_knee0 8192 3
pd pdmorph 0 16384
voice 0 pdmorph
hash 7d97f0b295cd023c
0 golden 0 ctl 0 -32768
0 golden 0 on 220000 32767
400000000 golden 0 ctl 0 32768
900000000 golden 0 ctl 0 0
1500000000 golden 0 off
