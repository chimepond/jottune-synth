timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
length_ns 1500000000
streams golden
patch modlfoself 0 65536 120 0 0 0 2 100 0 180
patch modlfoself 1 131072 0 1024 0 0 2 100 0 180
lfo modlfoself 1 3000 0 0 20 0 0 0 3 1 2
mod modlfoself 0 lfo lfo_rate 2000 0
mod modlfoself 1 ctl0 lfo_pitch 80 0
mod modlfoself 2 lfo index1 512 3
voice 0 modlfoself
hash f5dec11f690a2c10
0 golden 0 ctl 0 32768
0 golden 0 on 220000 32767
650000000 golden 0 on 220000 32767
800000000 golden 0 ctl 0 0
1100000000 golden 0 off
