timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1000000000
streams golden
patch pegdrop 0 65536 60 0 0 256 1 220 120 900
patch pegdrop 1 4194304 120 0 0 0 1 60 1200 60 0 0 4
route pegdrop 2 3
peg pegdrop 0 80 0 0 0 0 2400 120 3
voice 0 pegdrop
hash 893d97e172e613de
0 golden 0 on 55000 32767
250000000 golden 0 off
450000000 golden 0 on 61735 24000
490000000 golden 0 pitch 65406
700000000 golden 0 off
