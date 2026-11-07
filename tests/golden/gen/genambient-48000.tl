timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
gen_clock 64
length_ns 600000000000
streams golden
gen 0 pink 24301 8 4
gen 1 brown 48879 1 8 16
gen 2 multi 196609 1 16 256 2 6 24576
gen 3 henon 424242 64 16 394064967394918 84442493013197
patch bed0 0 65536 180 0 0 0 600 300 30 1400
patch bed0 1 131072 0 1200 0 0 10 300 0 100
patch bed1 0 65536 180 0 0 0 600 300 30 1400
patch bed1 1 131072 0 1200 0 0 10 300 0 100
patch bed2 0 65536 180 0 0 0 600 300 30 1400
patch bed2 1 131072 0 1200 0 0 10 300 0 100
filter bed0 lp 700000 850
filter bed1 lp 900000 850
filter bed2 lp 1100000 850
mod bed0 0 gen.0.x cutoff 2400 5
mod bed0 1 gen.3.x pitch0 40 5
mod bed1 0 gen.0.x cutoff 2400 5
mod bed1 1 gen.3.y pitch0 40 5
mod bed2 0 gen.0.x cutoff 2400 5
mod bed2 1 gen.3.x pitch0 40 5
voice 0 bed0
voice 1 bed1
voice 2 bed2
gen_lane 0 0 - 1 - 1536 4500 300 20000 1024 2741
gen_lane 1 1 - 2 - 2048 5200 300 20000 1280 2741
gen_lane 2 2 - 2 - 2560 5700 300 20000 1536 2741
hash ada87e2f48f3e937
120000000000 golden gen 0 reset 314159
240000000000 golden gen 3 b 84442493013197
360000000000 golden lane 1 enable 0
384000000000 golden lane 1 enable 1
480000000000 golden gen 2 gain 20000
