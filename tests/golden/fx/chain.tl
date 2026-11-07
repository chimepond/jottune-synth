# The master chain (the effects API, section 5): the DC blocker at its 100 Hz
# maximum, a fast limiter at -3 dB and the soft clip, on a 1:1 bass that
# carries DC and two loud tones over it. Mono through draft2 with the
# space off and blocks of 32: the limiter's half block is 16 samples.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1200000000
space off
block 32
streams fx
patch bass 0 65536 0 0 0 256 2 400 60 120
patch bass 1 65536 0 1536 0 128 1 300 120 100
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 bass
voice 1 tone
voice 2 tone
dcblock 100000
limiter 30 20
softclip
hash 3f5702a6849a7e7e
0 fx 0 on 55000 32767
200000000 fx 1 on 440000 30000
350000000 fx 2 on 659255 30000
700000000 fx 1 off
800000000 fx 2 off
900000000 fx 0 off
