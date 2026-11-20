# jottune-tl 1 1
# {
#   "license": "Apache-2.0",
#   "tempo": {
#     "max_bpm": 96,
#     "min_bpm": 96
#   },
#   "title": "A first conversation"
# }
# end-jottune-tl
timeline draft2
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 23000000000
streams lead bass chords
# A first conversation
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# Teaching arrangement: write your own melody before claiming original publication.
# The metadata license describes this Apache-2.0 teaching example, not CC0 publication consent.
# Eight bars, 4/4, quarter-note BPM 96; ordinary stereo, not binaural.
# Lead and bass remain centered. Independent chord voices sit left and right.
space stereo
block 64
near 100
gain 40
delay 312500 6500 3500
reverb 400 700
patch lead     0  65536     110        0         0            200         1         1100     1200         250         # recipe marimba: 1:3, a short bright strike (clockwork)
patch lead     1  196608    0          2458      0            200         1         80       600          100
patch bass     0  65536     130        0         0            128         30        3000     100          900         # recipe round-bass: 1:1 at a low index (first-light)
patch bass     1  65536     0          717       0            128         10        1500     120          900
patch chord    0  65536     170        0         0            180         2         2400     300          350         # recipe epiano: electric piano, 1:1; velocity opens the index, which decays faster than the level (waltz)
patch chord    1  65536     0          1638      0            220         1         700      240          350
voice 0 lead
voice 1 bass
voice 2 chord
voice 3 chord
0 lead 0 pos 0 1000 0
0 bass 1 pos 0 1000 0
0 chords 2 pos -600 800 0
0 chords 3 pos 600 800 0
0 lead 0 send reverb 2400
0 chords 2 send reverb 2400
0 chords 3 send reverb 2400
0 lead 0 send delay 3200
# 0 lead Hook and answer
0 bass 1 on 130813 23000
1250000000 bass 1 off
625000000 chords 2 on 329628 19500
1562500000 chords 2 off
937500000 chords 3 on 391995 18500
1562500000 chords 3 off
2500000000 bass 1 on 110000 23000
3750000000 bass 1 off
3125000000 chords 2 on 329628 19500
4062500000 chords 2 off
3437500000 chords 3 on 440000 18500
4062500000 chords 3 off
# 5000000000 lead Answer with space
5000000000 bass 1 on 87307 23000
6250000000 bass 1 off
5625000000 chords 2 on 349228 19500
6562500000 chords 2 off
5937500000 chords 3 on 440000 18500
6562500000 chords 3 off
7500000000 bass 1 on 97999 23000
8750000000 bass 1 off
8125000000 chords 2 on 293665 19500
9062500000 chords 2 off
8437500000 chords 3 on 493883 18500
9062500000 chords 3 off
# 10000000000 lead Quieter contrasting middle
10000000000 bass 1 on 110000 20500
11250000000 bass 1 off
10625000000 chords 2 on 261626 16000
11562500000 chords 2 off
10937500000 chords 3 on 329628 16000
11562500000 chords 3 off
12500000000 bass 1 on 87307 20500
13750000000 bass 1 off
13125000000 chords 2 on 261626 16000
14062500000 chords 2 off
13437500000 chords 3 on 349228 16000
14062500000 chords 3 off
# 15000000000 lead Return and cadence
15000000000 bass 1 on 97999 23000
16250000000 bass 1 off
15625000000 chords 2 on 293665 19500
16562500000 chords 2 off
15937500000 chords 3 on 493883 18500
16562500000 chords 3 off
17500000000 bass 1 on 130813 23000
19375000000 bass 1 off
18125000000 chords 2 on 329628 19500
19375000000 chords 2 off
18437500000 chords 3 on 391995 18500
19375000000 chords 3 off
0 lead 0 on 659255 25000
312500000 lead 0 off
468750000 lead 0 on 783991 23000
781250000 lead 0 off
937500000 lead 0 on 880000 24500
1562500000 lead 0 off
2500000000 lead 0 on 783991 23000
3125000000 lead 0 off
3437500000 lead 0 on 659255 21000
3750000000 lead 0 off
5000000000 lead 0 on 698456 24000
5312500000 lead 0 off
5468750000 lead 0 on 880000 22000
5781250000 lead 0 off
5937500000 lead 0 on 783991 24000
6562500000 lead 0 off
7500000000 lead 0 on 587330 22000
8125000000 lead 0 off
8437500000 lead 0 on 493883 20000
8750000000 lead 0 off
10000000000 lead 0 on 523251 20000
10937500000 lead 0 off
12500000000 lead 0 on 698456 20000
13437500000 lead 0 off
15000000000 lead 0 on 659255 25000
15312500000 lead 0 off
15468750000 lead 0 on 783991 23000
15781250000 lead 0 off
15937500000 lead 0 on 987767 24500
16562500000 lead 0 off
17500000000 lead 0 on 587330 22000
17812500000 lead 0 off
18125000000 lead 0 on 523251 23000
19375000000 lead 0 off
