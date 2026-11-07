timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch pegslide 0 65536 0 0 0 256 5 800 120 300
patch pegslide 1 65536 0 1536 0 0 5 600 200 300
patch pegslide 2 65536 0 512 0 0 2 300 400 300 0 0 0 0 1000000
route pegslide 3 1 1>0:256 2>0:256
lfo pegslide 0 5500 300 200 20 0 0 0 3 0 0
peg pegslide 50 400 -30 150 0 100 -1200 240 7
voice 0 pegslide
hash 9fcedaabd4149e29
0 golden 0 on 220000 32767
200000000 golden 0 on 246942 30000
950000000 golden 0 off
1050000000 golden 0 on 220000 32767
1250000000 golden 0 off
