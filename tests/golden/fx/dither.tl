# The TPDF dither under the soft clip (the effects API, section 5; ):
# golden/48000/bell.tl's bell, quiet (velocity 600: a peak of 899, -31 dBFS
# after the cubic's +3.52 dB), decaying through the last LSBs into silence
# that the dither keeps alive, -1, 0 or +1 LSB, to the record's end. The
# seed is the top of its range. Mono, draft1; the model draws the same
# sequence, so the last nonzero sample is equal.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams fx
patch bell 0 65536 0 0 0 256 2 2500 900 300
patch bell 1 229376 0 8192 0 0 1 1200 600 400
voice 0 bell
softclip
dither 4294967295
hash 59c95104c1cf17b1
0 fx 0 on 523250 600
1400000000 fx 0 off
