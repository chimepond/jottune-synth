timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1200000000
streams golden
patch freeop 0 65536 100 0 0 256 5 160 120 80 0 0 0 0 0 8192 2
patch freeop 1 65536 0 512 0 0 5 160 180 80 0 0 0 0 330000 16384 2
patch freeop 2 65536 140 0 0 256 5 160 160 80 0 0 0 0 440000 24576 2
patch freeop 3 131072 220 0 0 256 3 100 240 60 0 0 4 0 70000 16384 2
route freeop 4 13 1>0:256
voice 0 freeop
hash c805edbfd7f175eb
37000000 golden 0 on 220000 32767
280000000 golden 0 off
613000000 golden 0 on 330000 32767
920000000 golden 0 off
