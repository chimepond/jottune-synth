timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch fbpre 0 65536 60 0 256 0 5 600 120 200
patch fbpre 1 65536 0 1536 1536 0 5 800 400 200
route fbpre 2 1 1>0:256
fbtype fbpre 0 presquare
fbtype fbpre 1 pre
voice 0 fbpre
hash e15eefbfec5854a1
0 golden 0 on 146832 32767
1100000000 golden 0 off
