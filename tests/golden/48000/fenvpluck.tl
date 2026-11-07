timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch fenvpluck 0 65536 60 0 0 256 2 900 300 150
patch fenvpluck 1 65536 0 4096 256 0 2 900 200 150
filter fenvpluck lp 300000 1500
fenv fenvpluck 20 600 0 200 4800 0 261626
voice 0 fenvpluck
hash f173798b202e24ab
0 golden 0 on 130813 32767
10000000 golden 0 on 130813 24000
400000000 golden 0 off
750000000 golden 0 on 130813 32767
850000000 golden 0 filter 3000000 1500
1050000000 golden 0 on 146832 30000
1250000000 golden 0 off
