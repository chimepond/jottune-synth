timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch ampretrig 0 65536 60 0 0 0 2 100 0 2000
patch ampretrig 1 65536 0 1024 0 0 2 100 0 2000
amp ampretrig 5 200 120 300
voice 0 ampretrig
hash ea09c8133014f175
0 golden 0 on 220000 32767
900000000 golden 0 off
950000000 golden 0 on 220000 32767
