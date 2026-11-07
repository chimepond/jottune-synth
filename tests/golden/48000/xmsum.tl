timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1000000000
streams golden
patch xmsum 0 65536 0 0 0 256 5 700 150 300
patch xmsum 1 131072 0 1024 0 0 5 600 200 300
patch xmsum 2 196608 0 768 0 0 5 500 200 300
patch xmsum 3 65536 0 512 384 0 5 400 250 300 0 0 4
route xmsum 4 1 1>0:256 2>0:512 3>0:1024 3>1:512
voice 0 xmsum
hash d60ed8057ccc8db7
0 golden 0 on 220000 32767
600000000 golden 0 off
