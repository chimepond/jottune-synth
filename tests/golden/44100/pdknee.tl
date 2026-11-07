timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
streams golden
patch pdknee 0 65536 180 0 0 0 5 100 0 300
patch pdknee 1 65536 0 0 0 0 5 100 0 300
pd pdknee 0 8192
voice 0 pdknee
hash 7e3ce7561f5edfd3
0 golden 0 on 220000 32767
1500000000 golden 0 off
