# The DC blocker alone (the effects API, section 4) at its 5 Hz guide: two bass
# voices whose carrier and modulator run at 1:1, so a sideband falls at
# 0 Hz and the mix carries DC. Mono, draft1.
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 1500000000
streams fx
patch bass 0 65536 0 0 0 256 2 400 60 120
patch bass 1 65536 0 1536 0 128 1 300 120 100
voice 0 bass
voice 1 bass
dcblock 5000
hash a714a6c5b55233bb
0 fx 0 on 55000 16000
400000000 fx 1 on 82407 14000
900000000 fx 0 off
1100000000 fx 1 off
