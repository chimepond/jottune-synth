# The mono path of a draft2 record (the spatial API, section 5): the golden
# sine's header and events with space off replay to the sine's own hash.
# The block, near and pos statements are read, checked and carried into the
# record, and touch nothing.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
space off
block 32
near 250
streams golden
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 sine
hash e76dc5bad8783650
0 golden 0 on 440000 32767
0 golden 0 pos 1000 0 0
1000000000 golden 0 off
