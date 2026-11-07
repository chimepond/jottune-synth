timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1000000000
streams golden
patch route4 0 65536 60 0 0 256 5 800 150 300
patch route4 1 65536 0 1024 0 0 5 600 200 300
patch route4 2 131072 60 0 0 256 2 400 300 200
patch route4 3 458752 0 768 0 0 1 200 400 200
route route4 4 5 1>0:256 3>2:256
voice 0 route4
hash 5b0e3470034f5064
0 golden 0 on 261626 32767
600000000 golden 0 off
