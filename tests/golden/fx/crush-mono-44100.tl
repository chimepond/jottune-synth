# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# : six-bit held noise and a bass reduced to one sample in eight.
# Off-boundary changes roll at the next block, preserving all other voices.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams fx
patch hat 0 65536 180 0 0 256 1 30 1200 25 0 0 4
patch hat 1 65536 1200 0 0 0 0 0 1200 0
patch bass 0 65536 120 0 0 256 2 300 60 80
patch bass 1 65536 180 1024 0 128 1 150 120 50
voice 0 hat
voice 1 bass
crush
hash 5f98436c22dcaa43
0 fx 0 crush 6 1
0 fx 1 crush 24 8
0 fx 0 on 7000000 20000
0 fx 1 on 55000 22000
80000000 fx 0 off
250123456 fx 0 crush 7 5
260000000 fx 0 on 9000000 18000
340000000 fx 0 off
400123456 fx 1 crush 24 11
650000000 fx 1 off
850000000 fx 0 on 6000000 17000
900000000 fx 0 off
1000123456 fx 0 crush 24 1
