timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1000000000
streams golden
patch route3 0 65536 0 0 0 256 3 900 150 300
patch route3 1 131072 0 2048 0 0 3 700 200 300
patch route3 2 65536 0 1536 0 0 2 500 300 300
route route3 3 1 1>0:256 2>1:256
voice 0 route3
hash 40b3bae4bd112714
0 golden 0 on 220000 32767
600000000 golden 0 off
