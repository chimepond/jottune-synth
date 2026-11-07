# The master chain alone on the stereo layout (the effects API, section 5),
# blocks of 32: the DC blocker at 50 Hz, a fast limiter at -0.5 dB linked
# across the channels (a loud voice hard left also turns the right down)
# and the soft clip. No bus effect.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1200000000
space stereo
block 32
near 100
streams fx
patch bass 0 65536 0 0 0 256 2 400 60 120
patch bass 1 65536 0 1536 0 128 1 300 120 100
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 bass
voice 1 tone
voice 2 tone
dcblock 50000
limiter 5 30
softclip
hash 0a6ff8749bacdcaa
0 fx 0 pos 0 1000 0
0 fx 0 on 55000 32767
0 fx 1 pos -1000 0 0
200000000 fx 1 on 440000 32767
0 fx 2 pos 1000 0 0
400000000 fx 2 on 659255 20000
700000000 fx 1 off
900000000 fx 2 off
1000000000 fx 0 off
