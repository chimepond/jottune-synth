timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch modwide 0 65536 120 0 0 0 2 100 0 180
patch modwide 1 131072 0 0 0 0 2 100 0 180
peg modwide -4800 0 -4800 0 -4800 0 -4800 0 1
mod modwide 0 ctl0 pitch0 -4800 0
voice 0 modwide
hash ef23f673e3bc4cc9
0 golden 0 ctl 0 32768
0 golden 0 on 1500000000 32767
1100000000 golden 0 off
