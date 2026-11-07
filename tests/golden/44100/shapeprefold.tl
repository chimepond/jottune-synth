timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch shapeprefold 0 65536 120 0 0 0 2 100 0 120
patch shapeprefold 1 65536 0 1024 0 0 2 100 0 120
filter shapeprefold lp 1200000 2000
shape shapeprefold 140 fold pre
voice 0 shapeprefold
hash 68b08d836ab3b7db
0 golden 0 on 110000 32767
1100000000 golden 0 off
