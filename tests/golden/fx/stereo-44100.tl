# The stereo layout at 44100 Hz with blocks of 128 (the effects API): the
# chorus at its extremes (20 ms base, 5 ms depth, 3 Hz), the largest
# reverb room with full damping, and a slow limiter at -2 dB that the
# two pads' sum reaches. The pads sit northeast and northwest; one moves
# behind (to the centre) while its sends fall.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
space stereo
block 128
near 100
streams fx
patch pad 0 65536 0 0 0 256 300 1000 30 800
patch pad 1 65536 30 512 0 0 300 1000 30 800
voice 0 pad
voice 1 pad
chorus 20000 5000 3000
reverb 1000 1000
limiter 20 500
hash 70f99e749ad4c27e
0 fx 0 pos 707 707 0
0 fx 0 send chorus 32768
0 fx 0 send reverb 24000
0 fx 0 on 261626 32767
0 fx 1 pos -707 707 0
0 fx 1 send chorus 20000
0 fx 1 send reverb 32768
0 fx 1 on 329628 32767
600000000 fx 1 move 0 -1000 0 600000000
600000000 fx 1 send chorus 0
900000000 fx 1 send reverb 8000
1200000000 fx 0 off
1300000000 fx 1 off
