timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 800000000
streams golden
patch lforand 0 65536 60 0 0 256 5 180 120 120
patch lforand 1 131072 0 768 0 0 5 250 180 160
lfo lforand 6 10000 0 80 35 40 256 16384 3 1 2
voice 0 lforand
hash 61e6668ec6f22f68
0 golden 0 on 440000 32767
300000000 golden 0 on 493883 24000
450000000 golden 0 pitch 523251
650000000 golden 0 off
