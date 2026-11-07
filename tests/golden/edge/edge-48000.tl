# Edge inputs of  at 48 kHz: every patch field and event value at its
# limits, key scaling at its extremes, retriggers in every stage, pitch
# events to 0 Hz and to the top, a zero-length note and the repository
# review's extreme-frequency reproducer. The render must be defined
# (UBSan) and give this hash on every build and host (check.sh).
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 3000000000
streams edge
# every field at its maximum
patch max 0 4294967295 65535 65535 65535 65535 65535 65535 65535 65535 4294967295 65535
patch max 1 4294967295 65535 65535 65535 65535 65535 65535 65535 65535 4294967295 65535
# every field 0: 0 Hz, 1-sample segments
patch zero 0 0 0 0 0 0 0 0 0 0 0 0
patch zero 1 0 0 0 0 0 0 0 0 0 0 0
# feedback and index at 63.999 rad, 1-sample segments but the modulator's
# 65.5 s decay and release
patch wild 0 65536 60 0 65535 65535 0 0 0 0
patch wild 1 4294967295 0 65535 65535 65535 0 65535 0 65535
# key scaling at the steepest slope from a break of 0 mHz and of 1 mHz
patch ks0 0 65536 200 0 0 256 1 500 100 500 4294967295 65535
patch ks0 1 196608 0 4096 0 0 1 500 100 500 0 65535
patch ks1 0 65536 200 0 0 256 1 500 100 500
patch ks1 1 196608 0 4096 0 0 1 500 100 500 1 65535
# the review's reproducer: a carrier ratio of 65535.99998 at the top note
patch review 0 4294967295 120 0 0 0 1 100 0 100
patch review 1 65536 0 0 0 0 1 100 0 100
# every stage long enough to retrigger in
patch stages 0 65536 100 0 0 256 100 300 60 300
patch stages 1 131072 0 2048 512 0 50 300 120 300
voice 0 max
voice 1 zero
voice 2 wild
voice 3 ks0
voice 4 ks1
voice 5 review
voice 6 stages
hash b88a00445506a05e
0 edge 0 on 4294967295 65535
0 edge 1 on 0 0
0 edge 2 on 110000 65535
0 edge 3 on 1 32767
0 edge 4 on 4294967295 32767
0 edge 5 on 4294967295 32767
0 edge 6 on 440000 32767
# both operators in the attack , then the carrier in the attack and
# the modulator in the decay
20000000 edge 6 on 493883 24000
80000000 edge 6 on 523251 32767
100000000 edge 1 pitch 440000
200000000 edge 1 off
# both in the decay
250000000 edge 6 on 440000 20000
300000000 edge 1 on 4294967295 32767
400000000 edge 2 pitch 0
500000000 edge 0 pitch 0
500000000 edge 3 on 440000 32767
600000000 edge 2 pitch 4294967295
700000000 edge 2 off
# the carrier idle, the modulator in its release, a velocity of 1
800000000 edge 2 on 55000 1
# both in the sustain, then pitch events to 0 Hz, the top and back
900000000 edge 6 on 392000 32767
1000000000 edge 0 pitch 4294967295
1000000000 edge 3 on 4294967295 32767
1000000000 edge 6 pitch 0
1100000000 edge 6 pitch 4294967295
1200000000 edge 6 pitch 440000
1300000000 edge 6 off
# in the release
1400000000 edge 6 on 349228 32767
1500000000 edge 0 off
1500000000 edge 4 off
1500000000 edge 5 off
1700000000 edge 6 off
2000000000 edge 0 on 4294967295 65535
# from silence, and a zero-length note: the off at gain 0
2200000000 edge 6 on 329628 32767
2200000000 edge 6 off
2500000000 edge 6 on 440000 32767
2600000000 edge 6 off
# the last sample: a pitch event on voice 1, its ratio-0 operators at 0 Hz
2999999999 edge 1 pitch 1
