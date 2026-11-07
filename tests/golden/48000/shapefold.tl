timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch shapefold 0 65536 60 0 0 0 2 100 0 120
patch shapefold 1 65536 0 1024 0 0 2 100 0 120
filter shapefold lp 1200000 707 x2
shape shapefold 180 fold
voice 0 shapefold
hash c0b5cfd786686f2c
0 golden 0 on 110000 32767
1100000000 golden 0 off
