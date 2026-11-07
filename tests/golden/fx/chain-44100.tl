# The master chain at 44100 Hz with the other extremes (the effects API,
# section 5): the DC blocker at 1 mHz, the limiter at full scale with a
# 1 s release, and the soft clip. Mono through draft2 with the space off
# and blocks of 128.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
space off
block 128
streams fx
patch bass 0 65536 0 0 0 256 2 400 60 120
patch bass 1 65536 0 1536 0 128 1 300 120 100
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 bass
voice 1 tone
voice 2 tone
dcblock 1
limiter 0 1000
softclip
hash 7e841b884accff03
0 fx 0 on 65406 32767
250000000 fx 1 on 523251 32767
500000000 fx 2 on 783991 32767
800000000 fx 1 off
800000000 fx 2 off
1000000000 fx 0 off
