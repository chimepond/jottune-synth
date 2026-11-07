# The binaural route at first order and 44.1 kHz, the table's other order
# and rate (the spatial API, sections 11 to 16; ), no pinna, the
# listener turned 30 degrees to the right. One tone walks the compass at
# 1 m, 250 ms per point (11025 samples, off the 64-sample block
# boundaries): ahead, right, behind, left, up, then 2 m ahead right
# (-6 dB). With a table this record must give this hash on every build and
# host with block_indep=yes; without one it is refused (check.sh).
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
space binaural
block 64
near 100
order 1
yaw 30000
streams field
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 tone
hash 569063b8c9645249
0 field 0 on 440000 16000
0 field 0 pos 0 1000 0
250000000 field 0 pos 1000 0 0
500000000 field 0 pos 0 -1000 0
750000000 field 0 pos -1000 0 0
1000000000 field 0 pos 0 0 1000
1250000000 field 0 pos 1414 1414 0
1700000000 field 0 off
