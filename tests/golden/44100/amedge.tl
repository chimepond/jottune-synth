timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch amedge 0 65536 120 0 0 0 2 100 0 120
patch amedge 1 32768 0 0 0 0 2 100 0 120
route amedge 2 1 1>0:192:am
voice 0 amedge
hash 51de909fbbc47a7a
0 golden 0 on 220000 32767
450000000 golden 0 pitch 330000
700000000 golden 0 on 220000 24000
1100000000 golden 0 off
