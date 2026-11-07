timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
gen_clock 64
length_ns 6000000000
streams golden
gen 0 euclid 1 8 0 1 5 0 -
gen 1 euclid 2 8 0 1 7 0 -
gen 2 euclid 3 8 0 1 11 0 -
patch tagged 0 65536 180 0 0 0 8 300 30 80
patch tagged 1 131072 0 800 0 0 10 300 0 100
voice 0 tagged
voice 1 tagged
voice 2 tagged
gen_lane 0 0 0 - - 8 6000 0 18000 4 0
gen_lane 1 1 1 - - 8 6700 0 18000 4 0
gen_lane 2 2 2 - - 8 7400 0 18000 4 0
hash b0b312e02d9e2f18
