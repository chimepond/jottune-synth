timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch modmany 0 65536 120 0 0 0 2 100 0 180
patch modmany 1 131072 0 1024 0 0 2 100 0 180
mod modmany 0 ctl0 pitch0 4800 0
mod modmany 1 ctl1 pitch0 -4800 0
mod modmany 2 vel index1 1024 0
mod modmany 3 eg0 level0 60 1
voice 0 modmany
hash 1a724bf009afe07d
0 golden 0 ctl 0 32768
0 golden 0 ctl 1 32768
0 golden 0 on 220000 32767
400000000 golden 0 ctl 1 0
800000000 golden 0 ctl 0 -32768
1100000000 golden 0 off
