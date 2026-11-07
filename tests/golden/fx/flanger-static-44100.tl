# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# : chorus source schedule retained; 2.5 s, gain -60.
# Static inverted mono comb.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2500000000
streams fx
patch keys 0 65536 0 0 0 256 5 800 60 300
patch keys 1 131072 0 1024 0 128 2 600 80 300
patch pad 0 65536 0 0 0 256 300 1000 30 800
patch pad 1 65536 30 512 0 0 300 1000 30 800
voice 0 keys
voice 1 pad
flanger 2000 1000 0 20000 1
gain -60
hash fc4f3ebd80e9df7b
0 fx 0 send flanger 32768
0 fx 0 on 440000 7000
250000000 fx 1 send flanger 16384
250000000 fx 1 on 220000 6000
400000000 fx 0 send flanger 8192
700100000 fx 0 send flanger 32768
700100000 fx 0 on 523251 7000
1000000000 fx 1 send flanger 0
1100000000 fx 0 off
1200000000 fx 1 off
