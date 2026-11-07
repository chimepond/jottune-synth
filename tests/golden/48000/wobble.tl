timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch wobble 0 65536 0 0 0 256 5 600 100 200
patch wobble 1 65536 0 2048 0 0 5 800 150 250
patch wobble 2 131072 0 512 0 0 5 900 200 250
route wobble 3 1 1>0:256 2>1:256
lfo wobble 4 4000 100 200 0 0 2048 0 0 0 2
voice 0 wobble
hash 524fef1740d386dd
0 golden 0 on 98000 32767
1200000000 golden 0 off
