# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# BITS 25 is outside the authored range.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1000000
streams fx
patch p 0 65536 120 0 0 256 1 20 60 20
patch p 1 65536 1200 0 0 0 0 0 1200 0
voice 0 p
crush
0 fx 0 crush 25 1
