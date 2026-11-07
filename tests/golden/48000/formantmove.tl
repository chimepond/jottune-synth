timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams golden
patch formantmove 0 65536 240 0 0 0 5 100 0 300
patch formantmove 1 131072 240 0 0 0 5 100 0 300
patch formantmove 2 196608 240 0 0 0 5 100 0 300
route formantmove 3 7
filter formantmove lp 4000000 707
fenv formantmove 0 500 0 200 1200 0 261626
mod formantmove 0 ctl0 formant_center1 300000 0
formant formantmove 0 500000 2000 60
formant formantmove 1 1500000 3000 120
formant formantmove 2 2500000 4000 180
voice 0 formantmove
hash a9470f2db5385cc5
0 golden 0 on 220000 32767
600000000 golden 0 ctl 0 32768
1500000000 golden 0 off
