# The pan law (c) of  on pan.tl's moves (the spatial API, section 15):
# space.c's stereo law with the pan's left-right component narrowed by the
# width W = 19661 (0.6 in Q1.15), so a source hard to one side keeps
# (1 - 0.6) / 2 of its power in the far ear: an interaural level
# difference of at most 6.02 dB in place of the stereo law's silence. W is
# provisional . Ahead, behind, above and the centre sound as with
# the stereo law. The render must give this hash on every build and host
# with block_indep=yes (check.sh).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2500000000
space stereo
block 64
near 100
panlaw 19661
streams pan
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 tone
voice 1 sine
hash 784093be65b9025a
0 pan 0 on 440000 20000
0 pan 0 pos 0 1000 0
250000000 pan 0 pos 707 707 0
300000000 pan 1 pos -1000 0 0
300000000 pan 1 on 659255 16000
450000000 pan 1 off
500000000 pan 0 pos 1000 0 0
750000000 pan 0 pos 707 -707 0
1000000000 pan 0 pos 0 -1000 0
1250000000 pan 0 pos -1000 0 0
1500000000 pan 0 pos 0 0 1000
1750000000 pan 0 pos 2000 0 0
2000000000 pan 0 pos 50 0 0
2200000000 pan 0 off
