# The third-order field with Brown and Duda's pinna per voice (the spatial API,
# section 14), their D for listeners PB and NH. A tone moves in a straight
# line from ahead right, (1000, 1000, 0), to behind right, (1000, -1000, 0),
# over 1 s from 0.25 s: it crosses the side at 0.75 s, where the pinna's
# echoes stop (behind the ears only the direct path sounds). A second voice
# sounds up and ahead, (0, 700, 700), from 0.3 s; the listener's yaw turns
# it to the left at 1.3 s (90 degrees), behind at 1.6 s (180)
# and back ahead at 1.9 s, and the moving tone with it. The echo delays
# scale with fs, at most 48000 with the pinna. The render must give this
# hash on every build and host with block_indep=yes (check.sh); the double
# model in field_ref.c holds the pinna to  (harness --field-test).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2500000000
space amb3
block 64
near 100
pinna pbnh
streams field
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 tone
voice 1 sine
hash 6bc2a5e8588a66aa
0 field 0 on 440000 16000
0 field 0 pos 1000 1000 0
250000000 field 0 move 1000 -1000 0 1000000000
300000000 field 1 pos 0 700 700
300000000 field 1 on 659255 12000
1300000000 field 0 yaw 90000
1600000000 field 0 yaw 180000
1900000000 field 0 yaw 0
2200000000 field 0 off
2200000000 field 1 off
