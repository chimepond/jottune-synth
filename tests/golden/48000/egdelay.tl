timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch egdelay 0 65536 60 0 0 256 10 80 120 300
patch egdelay 1 131072 0 2048 0 0 10 100 180 350
eg egdelay 0 30 0 0
eg egdelay 1 60 0 0
voice 0 egdelay
hash 06ae945ffb689eae
0 golden 0 on 440000 32767
10000000 golden 0 on 493883 32767
20000000 golden 0 off
100000000 golden 0 on 440000 32767
200000000 golden 0 on 493883 32767
205000000 golden 0 on 523251 32767
650000000 golden 0 off
1050000000 golden 0 on 440000 32767
1250000000 golden 0 off
