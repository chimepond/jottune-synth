# The ping-pong delay (; the effects API, section 4): one line of 750 ms,
# the left return the tap at 375 ms and the right the tap at 750 ms, so
# the repeats land left, right, left as the far tap feeds back at 0.6
# through a 3 kHz low-pass and a 120 Hz high-pass. A pluck at the centre
# sends fully and a bass at the centre sends half: its repeats lose their
# low end each pass. A third voice northeast sends nothing; a sender's
# pos places its dry signal, never its echoes . Draft2, stereo,
# block 64.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 3500000000
space stereo
block 64
near 100
streams fx
patch pluck 0 65536 0 0 0 256 1 400 200 200
patch pluck 1 196608 0 2048 0 128 1 150 200 100
patch bass 0 65536 0 0 0 256 2 400 60 120
patch bass 1 65536 0 1536 0 128 1 300 120 100
voice 0 pluck
voice 1 bass
voice 2 pluck
pingpong 375000 19661 3000 120
hash 27e5cad71f8c5257
0 fx 0 pos 0 1000 0
0 fx 0 send delay 32768
0 fx 1 pos 0 1000 0
0 fx 1 send delay 16384
0 fx 2 pos 707 707 0
0 fx 0 on 440000 16000
250000000 fx 0 off
750000000 fx 1 on 55000 26000
1000000000 fx 1 off
1500000000 fx 0 on 659255 14000
1600000000 fx 2 on 329628 12000
1750000000 fx 0 off
1850000000 fx 2 off
