timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch modenv 0 65536 120 0 0 0 2 100 0 180
patch modenv 1 131072 0 1024 0 0 2 100 0 180
filter modenv lp 1000000 707
peg modenv 1200 200 0 300 0 0 -1200 150 3
fenv modenv 50 300 500 150 1200 0 261626
mod modenv 0 peg pitch1 1200 1
mod modenv 1 fenv index1 1024 0
mod modenv 2 note level0 120 0
voice 0 modenv
hash 749678475b3dceff
0 golden 0 on 220000 32767
300000000 golden 0 pitch 330000
700000000 golden 0 on 440000 32767
1100000000 golden 0 off
