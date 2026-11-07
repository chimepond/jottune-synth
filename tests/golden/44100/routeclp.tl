timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1000000000
streams golden
patch routeclp 0 65536 0 0 0 256 5 400 20 200
patch routeclp 1 65536 0 0 0 256 5 400 20 200
patch routeclp 2 131072 0 1024 0 256 5 400 20 200
route routeclp 3 7 2>0:256
filter routeclp bp 1000000 500
voice 0 routeclp
hash 309cfca78ef6fdff
0 golden 0 on 110000 32767
600000000 golden 0 off
