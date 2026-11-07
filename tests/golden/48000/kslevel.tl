timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1600000000
streams golden
patch kslevel 0 65536 120 0 0 256 5 200 60 150 440000 60
patch kslevel 1 131072 120 0 0 256 5 200 60 150 440000 90
patch kslevel 2 196608 120 0 0 256 5 200 60 150 440000 0
patch kslevel 3 65536 0 8192 0 256 5 300 60 150 440000 150
route kslevel 4 7 3>0:16
ks kslevel 2 60
kstype kslevel 0 level - -
kstype kslevel 1 level + -
kstype kslevel 2 level - +
kstype kslevel 3 index + -
voice 0 kslevel
hash 572e8519ea0554ef
0 golden 0 on 110000 16384
300000000 golden 0 off
500000000 golden 0 on 440000 16384
800000000 golden 0 off
1000000000 golden 0 on 1760000 16384
1300000000 golden 0 off
