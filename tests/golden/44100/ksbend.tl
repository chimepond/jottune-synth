timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 2000000000
streams golden
patch ksbend 0 65536 180 0 0 0 5 100 0 800
patch ksbend 1 65536 0 0 0 0 5 100 0 300
ksource ksbend 0 32700 7 2048
voice 0 ksbend
hash 5cb2658b1b7c284d
0 golden 0 on 110000 32767
0 golden 0 pitch 110000
200000000 golden 0 pitch 123471
400000000 golden 0 pitch 138591
600000000 golden 0 pitch 155563
800000000 golden 0 pitch 174614
900000000 golden 0 off
1000000000 golden 0 pitch 195998
1200000000 golden 0 pitch 220000
