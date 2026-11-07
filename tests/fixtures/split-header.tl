# alloy: a two-layer electric piano over a round bass, in C Dorian. 120 bpm, 4/4, sixteenths of
# 125 ms with the odd ones 12 ms late, 32 bars and a last hit. An original foundry-family piece
# whose hook is the family's first layered keys: an electric-piano body and, 1 ms later at 85
# per cent of the velocity, its tine with key scaling, playing r Eb5 r C5 Bb4 r G4 r F4 G4 r A4
# C5 over i and IV (Cm7 and F7, two bars each). Hats, a rim and a bongo open it, the bass (the
# round-bass recipe, moved short) enters at 3, the kick on G1, the clap with a snare body and
# the chorused piano stabs (the body recipe, short, each note on two voices 7 cents apart) at 5,
# the hook at 9. The break, 17 to 20, is the split: the kick and the bass out, the hook on the
# body alone for two bars and then on the tine alone for two, crackle under it and a riser
# under the tine. A2 from 21 answers the hook higher in 24 and 28, 29 to 32 are stabs and bass
# with a snare roll in 32, and the last hit is a Cm9 staggered 4 ms a note. The kick sweeps and
# the riser are pitch events ; the layered keys and the piano stabs are new to the
# family; no recipe is new, there is no new DSP and no spatial motion. Keys and mirror keep the
# mono balance and the lin30 placement.
# Render and play: tools/music/play.sh alloy
timeline draft1
engine a1 sine_bits=12 exp_bits=10 phase64=1 env64=1 fb_avg=1 tables=58021f1150d3c8d2
fs 48000
length_ns 68000000000
streams hook bass drums perc chords foley
#     name     op ratio_q16 level_db10 index_q10 feedback_q10 vel_sens_q8 attack_ms decay_ms sustain_db10 release_ms ks_break_millihz ks_slope_db10
patch body     0  65536     171        0         0            200         2         8000     500          200         # recipe ep-body (c_level +4, c_r -250, m_r -250): electric-piano body, 1:1, velocity opens the index; layer ep-tine on the same key (nocturne)
patch body     1  65536     0          1126      0            230         1         1400     140          200
patch tine     0  65536     271        0         0            256         1         1400     1200         150         # recipe ep-tine (c_level +4, m_ks_break +523.25, m_ks_slope +12, c_r -150, m_r -100): electric-piano tine: the modulator at 14 times the note, a metallic strike that dies away (nocturne)
patch tine     1  917504    0          1536      0            220         1         600      1200         100        523250           120
patch bass     0  65536     111        0         0            128         8         3000     100          150         # recipe round-bass (c_level -1, m_index +0.4, c_a -22, m_a -5, c_r -750, m_r -750): 1:1 at a low index (first-light)
patch bass     1  65536     0          1126      0            128         5         1500     120          150
patch kick     0  65536     41         0         0            128         1         600      1200         60          # the kick: a sine swept down two octaves onto its note by pitch events, a 1:1 click as it starts
patch kick     1  65536     0          2560      0            96          1         40       1200         30
patch clap     0  65536     111        0         0            128         1         450      1200         6           # recipe clap (c_level +2): at 1.2 kHz, short bursts 10 ms apart, then a held one for the tail (fiesta)
patch clap     1  92406     0          2867      3584         0           1         100      0            40
patch crash    0  65536     131        0         0            200         1         5000     1200         700         # recipe crash (c_level +2): at 5 kHz, about 24 dB a second (shuffle)
patch crash    1  92406     0          3072      3584         0           1         100      0            700
patch hat      0  65536     131        0         0            200         1         110      1200         25          # the closed hat: 1:1.41 at 3 rad fed back 1.3 rad, more metal than noise
patch hat      1  92406     0          3072      1331         0           1         100      0            25
patch open     0  65536     141        0         0            200         1         1100     1200         40          # the open hat: the closed hat ringing on, choked by the next one
patch open     1  92406     0          3072      1331         0           1         100      0            40
patch chirp    0  65536     141        0         0            160         1         160      1200         30          # a chirp: a 1:2 blip whose pitch falls onto its note by pitch events
patch chirp    1  131072    0          1229      0            120         1         60       180          30
patch stab     0  65536     211        0         0            200         2         2000     500          80          # recipe ep-body (c_level +8, c_d -6000, c_r -370, m_r -370): electric-piano body, 1:1, velocity opens the index; layer ep-tine on the same key (nocturne)
patch stab     1  65536     0          1126      0            230         1         1400     140          80
patch pad      0  65536     231        0         0            96          700       1500     30           600         # recipe warm-pad (c_level +2, c_r -1000, m_r -1200): 1:1, the modulator 0.15 % sharp for a slow shimmer (first-light)
patch pad      1  65634     0          1126      461          64          900       2500     60           600
patch crackle  0  65536     331        0         0            0           1         20       1200         10          # recipe pin-click (c_level -2): at 2.6 kHz, a breath of noise (music-box)
patch crackle  1  92406     0          2560      3584         0           1         100      0            10
patch riser    0  65536     331        0         0            0           3000      100      0            400         # recipe noise-tail (c_level -4, c_a +2600, c_r -1100): noise that follows a pitch curve; the modulator holds (night-sky's comet tail)
patch riser    1  92406     0          3072      3584         0           1         100      0            1500
patch snare    0  65536     111        0         0            180         1         110      1200         50          # recipe snare-body (c_level +4): at 190 Hz, 1:1.5, a short thud (shuffle)
patch snare    1  98304     0          1434      0            128         1         50       1200         40
patch rim      0  65536     201        0         0            200         1         70       1200         50          # recipe tock (c_level +7): at 1.3 kHz, the tick lower and longer (clockwork)
patch rim      1  92406     0          2560      3072         0           1         80       1200         50
patch bongo    0  65536     181        0         0            160         1         200      1200         80          # recipe wood-drum (c_level +5, c_d -250): 80:55 (Chowning), the index falling from 25 rad within a few ms (fiesta)
patch bongo    1  45056     0          25600     0            96          1         30       1200         20
voice  0 body      # the piano bodies
voice  1 body
voice  2 body
voice  3 body
voice  4 body
voice  5 body
voice  6 tine      # the piano tines, struck 1 ms after the bodies
voice  7 tine
voice  8 tine
voice  9 tine
voice 10 tine
voice 11 tine
voice 12 bass      # the bass
voice 13 bass
voice 14 bass
voice 15 kick      # the kick
voice 16 clap      # the clap
voice 17 crash     # the crash
voice 18 hat       # the closed hat
voice 19 open      # the open hat
voice 20 chirp     # chirps
voice 21 chirp
voice 22 stab      # the stabs
voice 23 stab
voice 24 stab
voice 25 stab
voice 26 stab
voice 27 stab
voice 28 stab
voice 29 stab
voice 30 pad       # the pad
voice 31 pad
voice 32 pad
voice 33 pad
voice 34 pad
voice 35 pad
voice 36 crackle   # crackle
voice 37 riser     # the riser
voice 38 snare     # the snare
voice 39 snare
voice 40 rim       # the rim
voice 41 rim
voice 42 bongo     # the bongo
voice 43 bongo
hash 578b9bb302982276
