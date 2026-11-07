# A tone moving in straight lines (the spatial API, section 4, ). Hard
# left, then across the front to hard right over 1 s from 0.25 s: sample
# 12000 is block 187.5 of 64 samples, so the move runs from block 188 to
# block 938 (sample 60000 is block 937.5). At 0.8 s (block 600, on a
# boundary) a second move cuts it and heads from where it is, (98, 0, 0),
# to 1 m ahead by block 900. At 1.3 s a move of 1 ns is an instant, as a
# pos: above, which sounds at the centre. At 1.4 s a pos replaces the move.
# At 1.5 s a move to 2 m right over 0.7 s runs past the end of the record
# and is cut there. A second voice sounds hard right and moves to hard left
# over 0.5 s with both ends off boundaries (samples 14880 and 38880: blocks
# 233 to 608). The render must give this hash on every build and host with
# block_indep=yes (check.sh); an independent model of the rule, written as
# a pos at every block boundary, gives the same hash (spike/README.md).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
space stereo
block 64
near 100
streams line
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 tone
voice 1 sine
hash ba4fabad4129a862
0 line 0 on 440000 20000
0 line 0 pos -1000 0 0
250000000 line 0 move 1000 0 0 1000000000
300000000 line 1 pos 1000 0 0
300000000 line 1 on 659255 16000
310000000 line 1 move -1000 0 0 500000000
800000000 line 0 move 0 1000 0 400000000
850000000 line 1 off
1300000000 line 0 move 0 0 1000 1
1400000000 line 0 pos 500 500 0
1500000000 line 0 move 2000 0 0 700000000
1900000000 line 0 off
