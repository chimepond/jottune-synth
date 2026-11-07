# The limiter alone (the effects API, section 5): three steady sines whose sum
# goes past full scale. The first, at half velocity, stays under the
# threshold (-1 dB); the second and third push the sum to about 2.5 times
# full scale; then they stop one by one and the gain recovers over the
# 100 ms release. Mono, draft1, block 64: the output runs one block late.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1600000000
streams fx
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 tone
voice 1 tone
voice 2 tone
limiter 10 100
hash 9443d7b7526d3b3e
0 fx 0 on 220000 16384
300000000 fx 1 on 277183 32767
600000000 fx 2 on 329628 32767
1000000000 fx 2 off
1200000000 fx 1 off
1400000000 fx 0 off
