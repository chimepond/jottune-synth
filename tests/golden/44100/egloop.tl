timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch egloop 0 65536 60 0 0 0 30 80 240 200
patch egloop 1 65536 0 1024 0 0 5 400 120 200
route egloop 2 1 1>0:256
eg egloop 0 0 60 360
egloop egloop 0
voice 0 egloop
hash a1a3915dcbbd906b
0 golden 0 on 220000 32767
1100000000 golden 0 off
