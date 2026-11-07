# Refused: pingpong together with delay ; both are the delay bus.
# Stereo, draft2.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 500000000
space stereo
block 64
near 100
streams fx
patch pluck 0 65536 0 0 0 256 1 400 200 200
patch pluck 1 196608 0 2048 0 128 1 150 200 100
voice 0 pluck
delay 187500 16384 2500
pingpong 375000 19661 3000 120
0 fx 0 send delay 32768
0 fx 0 on 440000 16000
250000000 fx 0 off
