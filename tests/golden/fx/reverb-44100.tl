# The reverb bus at 44100 Hz, Freeverb's own lengths, a large room with
# little damping (900 200); the keys' send falls from full to half at
# 0.6 s and to nothing at 1.0 s. Mono, draft1.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
streams fx
patch keys 0 65536 0 0 0 256 5 800 60 300
patch keys 1 131072 0 1024 0 128 2 600 80 300
voice 0 keys
reverb 900 200
hash 561f74ce17ed5408
0 fx 0 send reverb 32768
0 fx 0 on 329628 14000
300000000 fx 0 off
600000000 fx 0 send reverb 16384
600000000 fx 0 on 440000 14000
800000000 fx 0 off
1000000000 fx 0 send reverb 0
1000000000 fx 0 on 391995 14000
1200000000 fx 0 off
