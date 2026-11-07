timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1000000000
streams golden
patch routesat 0 65536 0 0 0 256 5 400 20 200
patch routesat 1 65536 0 0 0 256 5 400 20 200
patch routesat 2 131072 0 1024 0 256 5 400 20 200
route routesat 3 7 2>0:256
voice 0 routesat
hash 197d4111b656a94a
0 golden 0 on 110000 32767
600000000 golden 0 off
