# Refused: order is the binaural route's (the spatial API, section 16).
timeline draft2
space stereo
order 3
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1000000000
streams golden
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 sine
0 golden 0 on 440000 32767
