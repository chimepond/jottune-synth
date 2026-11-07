timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 44100
gen_clock 64
length_ns 8000000000
streams golden
gen 0 lsystem 99 32 0 0
gen_axiom 0 8 AB
gen_rule 0 A AB
gen_rule 0 B A
patch word 0 65536 180 0 0 0 8 300 30 80
patch word 1 131072 0 800 0 0 10 300 0 100
voice 0 word
gen_lane 0 0 - 0 - 32 6900 700 20000 24 2741
hash d8aba9ab02c09d7a
2000000000 golden gen 0 rot 7
5000000000 golden lane 0 enable 0
5500000000 golden lane 0 enable 1
