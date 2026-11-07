timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch modctl 0 65536 120 0 0 0 2 100 0 180
patch modctl 1 131072 0 1024 0 0 2 100 0 180
mod modctl 0 ctl0 pitch0 1200 0
voice 0 modctl
hash a8ea5c2e64d1d1a3
0 golden 0 ctl 0 32768
0 golden 0 on 220000 32767
250000000 golden 0 ctl 0 -32768
500000000 golden 0 pitch 330000
750000000 golden 0 ctl 0 0
1100000000 golden 0 off
