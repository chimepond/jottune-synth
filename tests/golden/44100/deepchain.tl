timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1000000000
streams golden
patch deepchain 0 65536 0 0 0 256 3 900 150 300
patch deepchain 1 131072 0 2048 0 0 3 700 200 300
patch deepchain 2 65536 0 1536 0 0 2 500 300 300
patch deepchain 3 196608 0 768 0 0 2 400 300 300
route deepchain 4 1 1>0:256 2>1:256 3>2:256
voice 0 deepchain
hash d2caadf57ee38d82
0 golden 0 on 110000 32767
600000000 golden 0 off
