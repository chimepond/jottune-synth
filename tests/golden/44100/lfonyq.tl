timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch lfonyq 0 65536 0 0 0 256 20 600 120 200
patch lfonyq 1 131072 0 1536 0 0 20 800 200 300
lfo lfonyq 0 5000 0 0 1200 0 0 0 3 0 0
voice 0 lfonyq
hash 46612770c2a46d46
0 golden 0 on 12000000 32767
1200000000 golden 0 off
