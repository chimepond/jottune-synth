timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1000000000
streams golden
patch routeback 0 65536 60 1024 0 256 5 700 150 300
patch routeback 1 196608 0 1024 0 0 5 500 200 300
patch routeback 2 131072 60 0 0 256 5 600 200 300
route routeback 3 5 0>2:384 1>0:256
voice 0 routeback
hash ce5dd18d4533574d
0 golden 0 on 196000 32767
600000000 golden 0 off
