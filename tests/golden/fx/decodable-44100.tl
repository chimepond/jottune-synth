# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
#  fixed stereo reverb/DC profile; fixture values pinned before rendering.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2500000000
space stereo
block 64
near 100
streams fx
patch keys 0 65536 0 0 0 256 5 800 60 300
patch keys 1 131072 0 1024 0 128 2 600 80 300
patch pluck 0 65536 0 0 0 256 1 400 200 200
patch pluck 1 196608 0 2048 0 128 1 150 200 100
voice 0 keys
voice 1 pluck
dcblock 5000
reverb 500 500
gain -60
decodable
hash 77d3d79469171ebf
0 fx 0 pos -100 100 0
0 fx 1 pos 100 100 0
0 fx 0 send reverb 32768
0 fx 1 send reverb 20000
0 fx 0 on 392000 14000
200000000 fx 0 off
400000000 fx 1 on 587330 12000
500000000 fx 1 off
700000000 fx 0 on 493883 14000
800000000 fx 0 off
