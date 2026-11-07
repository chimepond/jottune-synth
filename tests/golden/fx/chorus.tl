# The chorus bus (the effects API, sections 2 and 3): 15 ms base, 3 ms depth,
# 0.8 Hz. Keys send fully from the start, drop to a quarter at 0.4 s (on a
# 64-sample block boundary: sample 19200 is block 300) and come back at
# 0.7001 s (sample 33604.8, inside block 525: from block 526). A pad
# joins at 0.25 s with a half send (sample 12000 is block 187.5: from block
# 188) and sends nothing from 1 s. Mono, draft1: the return adds to the dry
# mix at unit gain.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams fx
patch keys 0 65536 0 0 0 256 5 800 60 300
patch keys 1 131072 0 1024 0 128 2 600 80 300
patch pad 0 65536 0 0 0 256 300 1000 30 800
patch pad 1 65536 30 512 0 0 300 1000 30 800
voice 0 keys
voice 1 pad
chorus 15000 3000 800
hash 349966ddcd5c9817
0 fx 0 send chorus 32768
0 fx 0 on 440000 7000
250000000 fx 1 send chorus 16384
250000000 fx 1 on 220000 6000
400000000 fx 0 send chorus 8192
700100000 fx 0 send chorus 32768
700100000 fx 0 on 523251 7000
1000000000 fx 1 send chorus 0
1100000000 fx 0 off
1200000000 fx 1 off
