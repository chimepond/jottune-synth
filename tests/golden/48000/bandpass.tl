timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams golden
patch bandpass 0 65536 60 0 0 256 5 900 150 2
patch bandpass 1 65536 0 4096 0 0 5 900 150 2
filter bandpass bp 1500000 2000
voice 0 bandpass
hash 2405c068af134fb5
0 golden 0 on 146832 32767
1200000000 golden 0 off
