timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1000000000
streams golden
patch routefb 0 65536 0 0 512 256 3 800 200 300
patch routefb 1 65536 0 1536 768 0 3 600 200 300
patch routefb 2 131072 0 1024 0 0 2 400 300 300
route routefb 3 1 1>0:256 2>1:256
voice 0 routefb
hash a84c07bfcedd64df
0 golden 0 on 146832 32767
600000000 golden 0 off
