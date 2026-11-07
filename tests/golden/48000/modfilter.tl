timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch modfilter 0 65536 120 0 0 0 2 100 0 180
patch modfilter 1 131072 0 1024 0 0 2 100 0 180
filter modfilter lp 500000 707
mod modfilter 0 ctl0 cutoff 4800 0
mod modfilter 1 ctl1 q 4000 2
voice 0 modfilter
hash c8559464dc461a31
0 golden 0 on 220000 32767
200000000 golden 0 ctl 0 32768
200000000 golden 0 ctl 1 32768
500000000 golden 0 filter 1000000 1000
700000000 golden 0 on 220000 32767
900000000 golden 0 off
