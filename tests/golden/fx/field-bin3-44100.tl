timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 500000000
space binaural
block 64
near 100
order 3
pinna pbnh
yaw 0
dcblock 17000
chorus 7000 1000 900
flanger 1000 800 700 7000 0
pingpong 50000 6000 5000 100
reverb 300 200
compressor 120 40 5 120 0
limiter 0 10
softclip
gain 0
dither 7
crush
streams f
patch p 0 65536 240 0 0 256 1 50 100 20
patch p 1 65536 0 0 0 0 1 50 0 20
voice 0 p
hash 8174717fa5d5955c
0 f 0 pos -500 1000 200
0 f 0 send chorus 12000
0 f 0 send flanger 10000
0 f 0 send delay 12000
0 f 0 send reverb 12000
0 f 0 crush 10 7
0 f 0 on 220000 12000
30000000 f 0 off
40012345 f 0 crush 12 5
60000000 f 0 move 500 1000 -100 120000000
80000000 f 0 yaw 90000
100000000 f 0 on 330000 10000
130000000 f 0 off
200000000 f 0 send delay 0
