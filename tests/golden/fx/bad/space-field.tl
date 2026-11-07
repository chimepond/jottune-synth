# Refused: the effects with a field route ( (2); drift review L07-4).
# Without the reverb line the record plays, as it does with space stereo,
# streams fx and fx events in place of the field ones.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 500000000
space amb1
block 64
near 100
streams field
patch sine 0 65536 0 0 0 256 5 500 200 800
patch sine 1 65536 0 0 0 0 5 500 200 800
voice 0 sine
reverb 700 300
0 field 0 on 440000 16384
0 field 0 pos 0 1000 0
