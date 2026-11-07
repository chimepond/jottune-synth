timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch filtertwo 0 65536 180 0 0 0 2 100 0 120
patch filtertwo 1 65536 0 4096 0 0 2 100 0 120
filter filtertwo lp 900000 2000 x2
voice 0 filtertwo
hash b1ec51f2cecc3be7
0 golden 0 on 220000 32767
300000000 golden 0 filter 2400000 707
500000000 golden 0 on 330000 32767
650000000 golden 0 off
