# golden/48000/bell.tl with the three bus effects on at the far ends of
# their ranges and no send (the effects API, section 2): the buses stay silent,
# the master has nothing to do, and the render must give bell.tl's hash,
# though it runs through the effects module.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 3000000000
streams golden
patch bell 0 65536 0 0 0 256 2 2500 900 300
patch bell 1 229376 0 8192 0 0 1 1200 600 400
voice 0 bell
chorus 7000 1000 100
delay 1000000 29491 20
reverb 0 0
hash 27dd1f8b6cdfcb8e
0 golden 0 on 523250 32767
2500000000 golden 0 off
