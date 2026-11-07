timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
streams golden
patch lowpass 0 65536 60 0 0 256 5 900 150 500
patch lowpass 1 65536 0 4096 0 0 5 900 150 500
filter lowpass lp 1200000 707
voice 0 lowpass
hash 7a38155e047c575a
0 golden 0 on 110000 32767
1200000000 golden 0 off
