timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams golden
patch formant3 0 65536 180 0 0 0 5 100 0 300 0 0 4
patch formant3 1 65536 0 0 0 0 5 100 0 300
formant formant3 0 500000 2000 60
formant formant3 1 1500000 3000 120
formant formant3 2 2500000 4000 180
voice 0 formant3
hash 460c2f93cf8c1fc9
0 golden 0 on 220000 32767
1500000000 golden 0 off
