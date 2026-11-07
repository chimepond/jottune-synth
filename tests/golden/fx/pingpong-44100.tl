# The ping-pong delay at 44100 Hz with blocks of 128 : 375 ms
# (16538 samples, the line twice that), feedback 0.6, a 2.5 kHz low-pass
# and no high-pass (0). The sender moves from the west to the east while
# it plays: its dry signal follows the pos, its echoes stay on the fixed
# taps, left then right . A second voice sends a third and is cut
# from the delay at 1 s, its echoes ringing on.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 3000000000
space stereo
block 128
near 100
streams fx
patch keys 0 65536 0 0 0 256 5 800 60 300
patch keys 1 131072 0 1024 0 128 2 600 80 300
patch pluck 0 65536 0 0 0 256 1 400 200 200
patch pluck 1 196608 0 2048 0 128 1 150 200 100
voice 0 keys
voice 1 pluck
pingpong 375000 19661 2500 0
hash 10f2566bb68c5611
0 fx 0 pos -1000 0 0
0 fx 0 send delay 32768
0 fx 0 move 1000 0 0 1000000000
0 fx 1 pos 0 1000 0
0 fx 1 send delay 10923
0 fx 0 on 392000 14000
0 fx 1 on 783991 10000
400000000 fx 0 off
400000000 fx 1 off
1000000000 fx 1 send delay 0
1000000000 fx 0 on 523251 14000
1100000000 fx 1 on 659255 10000
1300000000 fx 0 off
1300000000 fx 1 off
