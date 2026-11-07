# Every effect on the spatial module's stereo layout (the effects API,
# sections 2 to 5): the sends are taken before the pan, the chorus
# returns its two taps a quarter cycle apart, the delay one tap on both
# channels, the reverb Freeverb's left and right (+23) lines, all at the
# centre gain; then the DC blocker, the limiter linked across the two
# channels and the soft clip. A bass at the centre, keys moving from left
# to right while their sends change (on and off block boundaries), a
# pluck hard right with the delay. Draft2, block 64.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2500000000
space stereo
block 64
near 100
streams fx
patch bass 0 65536 0 0 0 256 2 400 60 120
patch bass 1 65536 0 1536 0 128 1 300 120 100
patch keys 0 65536 0 0 0 256 5 800 60 300
patch keys 1 131072 0 1024 0 128 2 600 80 300
patch pluck 0 65536 0 0 0 256 1 400 200 200
patch pluck 1 196608 0 2048 0 128 1 150 200 100
voice 0 bass
voice 1 keys
voice 2 pluck
dcblock 5000
chorus 12000 2000 500
delay 250000 16384 4000
reverb 700 300
limiter 10 50
softclip
hash 0a847fec63e42d5c
0 fx 0 pos 0 1000 0
0 fx 0 send reverb 6000
0 fx 0 on 55000 26000
0 fx 1 pos -1000 0 0
0 fx 1 send chorus 32768
0 fx 1 send reverb 16384
0 fx 1 on 440000 20000
0 fx 1 move 1000 0 0 1500000000
0 fx 2 pos 1000 0 0
0 fx 2 send delay 32768
0 fx 2 send reverb 8000
250000000 fx 2 on 659255 18000
500000000 fx 1 send chorus 4000
500000000 fx 2 on 587330 18000
750100000 fx 1 send reverb 32768
750100000 fx 2 on 493883 18000
1000000000 fx 1 send chorus 32768
1000000000 fx 0 on 82407 30000
1200000000 fx 2 send delay 0
1500000000 fx 1 off
1600000000 fx 0 off
1700000000 fx 2 off
