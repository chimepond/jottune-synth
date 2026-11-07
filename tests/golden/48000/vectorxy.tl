timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams golden
patch vectorxy 0 65536 180 0 0 0 5 100 0 300
patch vectorxy 1 131072 180 0 0 0 5 100 0 300
patch vectorxy 2 196608 180 0 0 0 5 100 0 300
patch vectorxy 3 262144 180 0 0 0 5 100 0 300
route vectorxy 4 15
lfo vectorxy 1 1000 0 0 0 0 0 0 0 0 0
peg vectorxy 1200 400 -1200 600 0 200 0 300 15
mod vectorxy 0 lfo vector_x 16384 3
mod vectorxy 1 peg vector_y 16384 3
vector vectorxy 16384 16384
voice 0 vectorxy
hash fb23af8262d4533b
0 golden 0 on 220000 32767
1500000000 golden 0 off
