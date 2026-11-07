# The soft clip alone (the effects API, section 5): one sine at a quarter of
# full scale, where the cubic's small-signal gain of 1.5 shows, then a
# second that takes the sum past full scale into the clamp. Mono, draft1.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1200000000
streams fx
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 tone
voice 1 tone
softclip
hash 8c8f2780b21e4d4d
0 fx 0 on 440000 8192
300000000 fx 0 off
400000000 fx 0 on 440000 20000
600000000 fx 1 on 554365 20000
1000000000 fx 1 off
1000000000 fx 0 off
