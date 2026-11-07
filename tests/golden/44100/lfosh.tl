timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch lfosh 0 65536 0 0 0 256 5 700 100 200
patch lfosh 1 262144 0 1024 0 0 5 700 200 200 0 0 4
patch lfosh 2 65536 0 768 0 0 5 700 200 200 0 0 0 0 50000
route lfosh 3 1 1>0:256 2>0:256
lfo lfosh 5 8000 0 0 1200 0 0 0 7 0 0
voice 0 lfosh
hash 8552d4b521cfe5f1
0 golden 0 on 220000 32767
1200000000 golden 0 off
