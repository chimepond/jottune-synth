timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch ampring 0 65536 60 0 0 0 2 100 0 2000
patch ampring 1 65536 0 1024 0 0 2 100 0 2000
filter ampring lp 900000 2000
amp ampring 5 200 120 300
voice 0 ampring
hash 1440211a83e8d0cb
0 golden 0 on 220000 32767
600000000 golden 0 off
