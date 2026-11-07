# The binaural route (the spatial API, sections 11 to 16; ): the
# third-order field (no order statement: 3) decoded to two ears by the
# build's filter table (make BINAURAL_H=PATH), with the pinna per voice
# (their D for listeners PB and NH). A tone moves from ahead right round
# behind, (1000, 1000, 0) to (1000, -1000, 0) over 1 s from 0.25 s; a
# second voice sounds up and ahead, (0, 700, 700), from 0.3 s; the
# listener's yaw turns the field 90 degrees at 1.3 s, 180 at 1.6 s and back
# at 1.9 s. Each ear is the sum of the field's channels convolved with
# the table's 256 taps, so the output starts 128 samples late (the
# filters' latency). With a table this record must give this hash on
# every build and host with block_indep=yes; without one it is refused
# (check.sh).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2500000000
space binaural
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
hash 7d533a31866a98e3
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
