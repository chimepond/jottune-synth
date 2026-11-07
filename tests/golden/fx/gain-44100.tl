# The master gain at its top, +24 dB (the effects API, section 5; ), in
# the stereo layout at 44100 Hz: two quiet tones placed northeast and
# west, the gain's largest multiplier ahead of the DC blocker and a
# limiter at -1 dB that the boosted sum reaches. The limiter's detector
# is linked, so the placed tones keep their directions while it works;
# the western tone moves to behind the listener under it .
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
space stereo
block 64
near 100
streams fx
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 tone
voice 1 tone
dcblock 20000
limiter 10 200
gain 240
hash ad9b2232395e0c75
0 fx 0 pos 707 707 0
0 fx 0 on 220000 2000
0 fx 1 pos -1000 0 0
300000000 fx 1 on 330000 2600
600000000 fx 1 move 0 -1000 0 400000000
1100000000 fx 0 off
1200000000 fx 1 off
