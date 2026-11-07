timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch ftrack 0 65536 60 0 0 256 2 900 300 150
patch ftrack 1 65536 0 4096 256 0 2 900 200 150
filter ftrack lp 300000 1500
fenv ftrack 20 600 0 200 4800 1000 261626
voice 0 ftrack
hash c57e0b67e771619d
0 golden 0 on 130813 32767
170000000 golden 0 off
370000000 golden 0 on 261626 32767
540000000 golden 0 off
740000000 golden 0 on 523251 32767
910000000 golden 0 off
1110000000 golden 0 on 1046502 32767
1280000000 golden 0 off
