# Refused: a send level past 32768 (unity).
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 500000000
streams fx
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 sine
reverb 500 500
0 fx 0 on 440000 16384
0 fx 0 send reverb 32769
