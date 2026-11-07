timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch syncturn 0 180224 120 0 0 0 2 100 0 120 0 0 0 0 0 16384
patch syncturn 1 65536 0 0 0 0 2 100 0 120
route syncturn 2 1 1>0:256:sync
voice 0 syncturn
hash c279f517d18996b1
0 golden 0 on 220000 32767
450000000 golden 0 pitch 330000
700000000 golden 0 on 220000 32767
1100000000 golden 0 off
