timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1000000000
streams golden
patch phase 0 65536 80 0 0 256 3 200 60 80 0 0 0 0 0 16384
patch phase 1 131072 0 1024 0 0 3 300 120 80 0 0 0 0 0 16384
patch phase 2 98304 80 0 0 256 3 200 60 80 0 0 0 0 0 0 1
patch phase 3 65536 160 0 0 256 3 150 200 80 0 0 4 0 0 0 1
route phase 4 13 1>0:256
voice 0 phase
hash 402d8aff90c13587
0 golden 0 on 220000 32767
300000000 golden 0 off
550000000 golden 0 on 220000 32767
850000000 golden 0 off
