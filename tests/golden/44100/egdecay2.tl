timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1800000000
streams golden
patch egdecay2 0 65536 60 0 0 256 10 80 120 300
patch egdecay2 1 131072 0 2048 0 0 10 100 180 350 0 0 0 3
eg egdecay2 0 0 240 480
eg egdecay2 1 0 300 600
voice 0 egdecay2
hash 0af3d54aad20a84d
0 golden 0 on 440000 32767
250000000 golden 0 on 440000 32767
600000000 golden 0 off
800000000 golden 0 on 329628 32767
900000000 golden 0 off
