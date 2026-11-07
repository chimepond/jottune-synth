# The TPDF dither in the stereo layout at 44100 Hz (the effects API, section
# 5; ): a quiet decaying bell (velocity 400: a peak of 369, -39 dBFS)
# placed northeast, moving northwest as it fades, on the plain rounding with
# `dither 1234567`. Each frame draws left then right, so the two
# channels' dither differs; the silence after the release keeps a TPDF of
# -1, 0 and +1 LSB on both channels to the record's end (: the
# dither is per channel after the pan).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
space stereo
block 64
near 100
streams fx
patch bell 0 65536 0 0 0 256 2 2500 900 300
patch bell 1 229376 0 8192 0 0 1 1200 600 400
voice 0 bell
dither 1234567
hash e787bf76ccfd6117
0 fx 0 pos 707 707 0
0 fx 0 on 523250 400
500000000 fx 0 move -707 707 0 800000000
1400000000 fx 0 off
