# The first-order field with the pinna's five D given in thousandths
# (the spatial API, section 14) and the listener turned 45 degrees to the
# left (the yaw statement), at 44.1 kHz, where the echo delays in samples
# are 0.919 of 48 kHz's. One tone walks the compass at 1 m, 250 ms per
# point (11025 samples, off the 64-sample block boundaries): ahead (heard
# 45 degrees right), right, behind, left (heard 45 degrees left), then up,
# and down behind. The render must give this hash on every build and host
# with block_indep=yes (check.sh).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
space amb1
block 64
near 100
pinna 1000 800 600 400 200
yaw -45000
streams field
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 tone
hash 635c534ec86be332
0 field 0 on 440000 16000
0 field 0 pos 0 1000 0
250000000 field 0 pos 1000 0 0
500000000 field 0 pos 0 -1000 0
750000000 field 0 pos -1000 0 0
1000000000 field 0 pos 0 0 1000
1250000000 field 0 pos 0 -707 -707
1700000000 field 0 off
