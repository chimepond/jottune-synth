timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 3000000000
streams golden
patch fretrig 0 65536 60 0 0 256 20 600 120 400
patch fretrig 1 131072 0 3072 0 0 10 600 200 400
filter fretrig bp 800000 1500
voice 0 fretrig
hash 6eced5f91e64e493
0 golden 0 on 196000 32767
300000000 golden 0 filter 2400000 1500
600000000 golden 0 on 261626 28000
1000000000 golden 0 off
1100000000 golden 0 filter 3000000 4000
1800000000 golden 0 on 196000 32767
2200000000 golden 0 off
