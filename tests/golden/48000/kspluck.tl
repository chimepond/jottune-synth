timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 2000000000
streams golden
patch kspluck 0 65536 180 0 0 0 5 100 0 800
patch kspluck 1 65536 0 0 0 0 5 100 0 300
ksource kspluck 0 32700 7 2048
voice 0 kspluck
hash aa05e618613997a4
0 golden 0 on 110000 32767
900000000 golden 0 off
