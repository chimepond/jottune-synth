# /: existing limiter three-tone schedule, extended to2s.
# Fast ratio20:1/attack0, compressor alone.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams fx
patch tone 0 65536 0 0 0 256 5 500 0 300
patch tone 1 65536 0 0 0 0 5 500 0 300
voice 0 tone
voice 1 tone
voice 2 tone
compressor 120 200 0 120 60
hash 7e0a3b30cb9e1866
0 fx 0 on 220000 16384
300000000 fx 1 on 277183 32767
600000000 fx 2 on 329628 32767
1000000000 fx 2 off
1200000000 fx 1 off
1400000000 fx 0 off
