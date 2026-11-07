timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1600000000
streams golden
patch pinkfree 0 65536 60 0 0 256 5 200 180 200
patch pinkfree 1 65536 0 512 0 0 5 300 240 250 0 0 9 0 6000000 0 2
patch pinkfree 2 65536 120 0 0 256 5 200 180 200 0 0 9 0 0 0 2
route pinkfree 3 5 1>0:256
voice 0 pinkfree
hash 3cddfb56a09b14fc
37000000 golden 0 on 220000 32767
250000000 golden 0 on 246942 32767
600000000 golden 0 off
1037000000 golden 0 on 329628 32767
1300000000 golden 0 off
