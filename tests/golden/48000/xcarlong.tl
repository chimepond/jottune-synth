timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams golden
patch xcarlong 0 65536 60 0 0 256 5 300 100 50
patch xcarlong 1 131072 0 1024 0 0 5 300 200 50
patch xcarlong 2 98304 60 0 512 256 5 600 100 800 0 0 4
route xcarlong 3 5 1>0:256
voice 0 xcarlong
hash a57b78e97b40ce0b
0 golden 0 on 196000 32767
300000000 golden 0 off
