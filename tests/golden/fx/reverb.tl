# The reverb bus (the effects API, sections 2 and 3): Freeverb's eight combs
# and four allpasses at their default room size and damping (500 500),
# lengths scaled to 48000 Hz. Two short keys hits and a pluck send, the
# tail rings on to 2.5 s. Mono, draft1.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2500000000
streams fx
patch keys 0 65536 0 0 0 256 5 800 60 300
patch keys 1 131072 0 1024 0 128 2 600 80 300
patch pluck 0 65536 0 0 0 256 1 400 200 200
patch pluck 1 196608 0 2048 0 128 1 150 200 100
voice 0 keys
voice 1 pluck
reverb 500 500
hash a7c3d9eab433bbc2
0 fx 0 send reverb 32768
0 fx 1 send reverb 20000
0 fx 0 on 392000 14000
200000000 fx 0 off
400000000 fx 1 on 587330 12000
500000000 fx 1 off
700000000 fx 0 on 493883 14000
800000000 fx 0 off
