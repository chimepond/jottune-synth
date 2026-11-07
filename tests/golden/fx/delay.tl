# The delay bus (the effects API, sections 2 and 3): 187.5 ms (a dotted eighth
# at 120 bpm), feedback 0.7, a 2.5 kHz low-pass in the loop. Plucks on the
# beat send fully; a second voice's send rises from a third to full at
# 1.0 s and the echoes ring on after the notes stop. Mono, draft1.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams fx
patch pluck 0 65536 0 0 0 256 1 400 200 200
patch pluck 1 196608 0 2048 0 128 1 150 200 100
voice 0 pluck
voice 1 pluck
delay 187500 22938 2500
hash 6053a1bd8f81f709
0 fx 0 send delay 32768
0 fx 0 on 220000 14000
0 fx 1 send delay 10923
250000000 fx 1 on 329628 12000
500000000 fx 0 on 246942 14000
750000000 fx 1 on 293665 12000
1000000000 fx 1 send delay 32768
1000000000 fx 0 on 220000 14000
1250000000 fx 1 on 329628 12000
1300000000 fx 0 off
1350000000 fx 1 off
