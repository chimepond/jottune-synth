# The third-order field turned by the listener (the spatial API, sections
# 11 to 13): sixteen AmbiX channels per frame. The record starts turned
# 30 degrees to the right (the yaw statement). A tone sounds up and to
# the northeast, (600, 700, 300); a second voice joins hard left at 0.3 s.
# The listener's yaw events (voice 0 always: the listener is not a voice)
# set the turn, not add to it, at the next 64-sample block boundary: 90
# degrees at 0.25 s (sample 12000, block 187.5, so from block 188), 180 at
# 0.5 s (block 375, on a boundary), -90, a full turn (sounds as 0), 45.5
# (between the exact quarter turns), -135.25 while the second voice moves
# up and to the right, and back to 0. The render
# must give this hash on every build and host with block_indep=yes
# (check.sh); the double model in field_ref.c holds the yaw to
# (harness --field-test).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2500000000
space amb3
block 64
near 100
yaw 30000
streams field
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 tone
voice 1 sine
hash 56da23011256b9c9
0 field 0 on 440000 16000
0 field 0 pos 600 700 300
250000000 field 0 yaw 90000
300000000 field 1 pos -1000 0 0
300000000 field 1 on 659255 12000
500000000 field 0 yaw 180000
750000000 field 0 yaw -90000
1000000000 field 0 yaw 360000
1250000000 field 0 yaw 45500
1300000000 field 1 move 1000 0 500 500000000
1500000000 field 0 yaw -135250
1750000000 field 0 yaw 0
2200000000 field 0 off
2200000000 field 1 off
