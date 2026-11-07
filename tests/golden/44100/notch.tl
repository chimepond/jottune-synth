timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
streams golden
patch notch 0 65536 60 0 0 256 5 900 150 500
patch notch 1 65536 0 4096 0 0 5 900 150 500
filter notch notch 1000000 1000
voice 0 notch
hash 35224156dd6e940f
0 golden 0 on 110000 32767
1200000000 golden 0 off
