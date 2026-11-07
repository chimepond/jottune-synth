# One steady tone at 1 m walked round the eight compass points, 500 ms
# each on 64-sample block boundaries (24000 samples is 375 blocks), after
# 500 ms at the centre to settle: n, ne, e, se, s, sw, w, nw, then off. The
# rear half folds onto the front (the spatial API, section 3), so s sounds as
# n, se as ne and sw as nw. The summed-channel power of each stretch is the
# flatness measurement of section 9 (spike/README.md); the hash is checked
# on every build (check.sh).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 5000000000
space stereo
block 64
near 100
streams walk
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 tone
hash 7ca379bec9abaeea
0 walk 0 on 440000 20000
500000000 walk 0 pos 0 1000 0
1000000000 walk 0 pos 707 707 0
1500000000 walk 0 pos 1000 0 0
2000000000 walk 0 pos 707 -707 0
2500000000 walk 0 pos 0 -1000 0
3000000000 walk 0 pos -707 -707 0
3500000000 walk 0 pos -1000 0 0
4000000000 walk 0 pos -707 707 0
4500000000 walk 0 off
