# /(15): a pre-pan kick detector ducks the placed bass.
# Zero makeup isolates attenuation; the linked gain preserves stereo placement.
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
space stereo
block 64
near 100
streams fx
patch kick 0 65536 0 0 0 256 1 160 120 80
patch kick 1 65536 0 0 0 0 0 0 0 0
peg kick 0 45 0 0 0 0 2400 0 1
patch bass 0 65536 180 0 0 256 5 500 0 180
patch bass 1 65536 0 0 0 0 0 0 0 0
voice 0 kick
voice 1 bass
compressor 120 40 5 120 0
hash 6adf453f5f246e88
0 fx 0 pos -100 100 0
0 fx 1 pos 100 100 0
0 fx 0 send duck 32768
0 fx 0 on 55000 32767
0 fx 1 on 55000 32767
80000000 fx 0 off
400000000 fx 0 on 55000 32767
480000000 fx 0 off
800000000 fx 0 on 55000 32767
880000000 fx 0 off
1200000000 fx 0 on 55000 32767
1280000000 fx 0 off
1400000000 fx 1 off
