# The first-order field (the spatial API, sections 11 and 12): four AmbiX
# channels per frame, W Y Z X (ACN order, SN3D gains). One steady tone at
# 1 m ahead, then every 250 ms northeast, hard right, behind, above, below,
# 2 m left (-6 dB) and 50 mm right (inside near: the gains shrink towards
# W alone), alternately on and off a 64-sample block boundary. A second
# voice starts hard left at 0.3 s and moves across the front to hard right
# over 1 s from 0.31 s, both ends off boundaries (samples 14880 and 62880:
# blocks 233 to 983). The render must give this hash on every build and
# host with block_indep=yes (check.sh); the double model in field_ref.c
# holds the same paths to  (harness --field-test).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2500000000
space amb1
block 64
near 100
streams field
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 tone
voice 1 sine
hash cb5ded915f23a0ed
0 field 0 on 440000 16000
0 field 0 pos 0 1000 0
250000000 field 0 pos 707 707 0
300000000 field 1 pos -1000 0 0
300000000 field 1 on 659255 12000
310000000 field 1 move 1000 0 0 1000000000
500000000 field 0 pos 1000 0 0
750000000 field 0 pos 0 -1000 0
1000000000 field 0 pos 0 0 1000
1250000000 field 0 pos 0 0 -1000
1500000000 field 0 pos -2000 0 0
1600000000 field 1 off
1750000000 field 0 pos 50 0 0
2200000000 field 0 off
