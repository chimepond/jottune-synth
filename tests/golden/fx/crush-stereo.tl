# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# /: the crushed source moves; its reverb send hears the insert.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 3000000000
space stereo
block 128
near 100
streams fx
patch pluck 0 65536 180 0 0 256 1 250 160 80
patch pluck 1 196608 120 2048 0 128 1 150 200 60
voice 0 pluck
reverb 300 500
crush
hash e283bf31a1fdb8f3
0 fx 0 pos -707 707 0
0 fx 0 send reverb 24000
0 fx 0 crush 6 7
0 fx 0 on 220000 22000
200000000 fx 0 off
400123456 fx 0 crush 8 5
450000000 fx 0 move 707 707 0 700000000
500000000 fx 0 on 329628 20000
700000000 fx 0 off
1000000000 fx 0 on 440000 18000
1200000000 fx 0 off
