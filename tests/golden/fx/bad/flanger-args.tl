# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# Refused: flanger-args.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2500000000
streams fx
patch keys 0 65536 0 0 0 256 5 800 60 300
patch keys 1 131072 0 1024 0 128 2 600 80 300
patch pad 0 65536 0 0 0 256 300 1000 30 800
patch pad 1 65536 30 512 0 0 300 1000 30 800
voice 0 keys
voice 1 pad
flanger 2000 1000 300 20000
0 fx 0 on 440000 7000
