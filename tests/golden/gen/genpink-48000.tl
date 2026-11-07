timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
gen_clock 64
length_ns 8000000000
streams golden
gen 0 pink 12345 1 16
patch motion 0 65536 180 0 0 0 8 300 30 80
patch motion 1 131072 0 800 0 0 10 300 0 100
filter motion lp 900000 900
mod motion 0 gen.0.x cutoff 4800 3
voice 0 motion
hash 17c5d984d0532e69
1000000000 golden 0 on 220000 24000
3000000000 golden 0 off
4000000000 golden 0 on 330000 24000
5000000000 golden gen 0 reset 42
6500000000 golden 0 off
