#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Convert a Standard MIDI File into a timeline piece (a host tool).

The library renders what a timeline or its caller asks for. It reads no
MIDI and manages no voices. Live voice allocation and stealing belong to
the app; this tool
is its deterministic offline form. It reads a whole file and makes, by
fixed rules, each choice a live manager makes as it goes: which voice a
note takes, which note a full patch cuts short, when a pedal lets a note
go. It writes the result as an ordinary piece of streams, patches, voices
and events, levelled to a -1.5 dBFS peak and hashed as compose.py levels
and hashes its pieces. The same file with the same options gives the same
bytes.

From the repository root:

    python3 -B tools/music/midi2tl.py SONG.mid --map 1=epiano
    python3 -B tools/music/midi2tl.py SONG.mid --map 1=epiano,2=round-bass:bass --dry-run
    python3 -B tools/music/midi2tl.py SONG.mid --map "1=epiano:keys m_index=-0.5" \
        --program 1=epiano,33=round-bass --filter 1=lp:2400:0.8 --voices 6
    python3 -B tools/music/midi2tl.py --selftest

Options (--map, --program, --filter, --mono, --glide, --unison, --kit,
--choke, --random, --ctl, --mpe, --scale, --arp, --chord, --arp-latch,
--arp-seed, --arp-cond, --arp-nei, --reserve and --limit may be given more than once, and each
takes entries separated by commas):

  --map CH=RECIPE[:STREAM] [ARG=DELTA ...]
      Play MIDI channel CH (1 to 16) with a recipe of recipes.txt
      (compose.py --recipes lists them), moved by the deltas a recipe
      statement takes: time=X and any tweak recipe takes (c_ARG=DELTA,
      m_ARG=DELTA, a routed recipe's o2_ARG, o3_ARG and wF_T). The
      channel's notes go to one stream, named chCH unless STREAM names it;
      stream names must differ in more than case (Channels, below). With
      @LO-HI[/VLO-VHI[~]] selects keys and optional velocities
      (Layers and splits); ~ opts into the bounded crossfade.
  --program PROG=RECIPE [ARG=DELTA ...]
      Switch patches on program changes (PROG 1 to 128, as General MIDI
      counts them). Without --program they are counted and ignored.
  --filter CH=MODE:HZ:Q
      Give every patch of mapped channel CH the voice filter : MODE
      lp, bp, hp or notch, cutoff HZ, resonance Q. Only then does CC 74
      move a cutoff.
  --voices N      voices a patch may use, 1 to 64 (default 8)
  --bend ST       the pitch-bend range in semitones, 0 to 127 (default 2)
  --tuning FILE   128 integers, the millihertz of MIDI keys 0 to 127 in
                  order (# starts a comment), in place of 12-TET
  --tail MS       the time after the later of the last event and the end
                  of the file (default 1000 ms plus 1.25 times the longest
                  carrier release among the patches used, rounded up)
  --mono CH=MODE[:PRIORITY]
      Play channel CH on one voice (Mono): MODE retrigger or legato,
      PRIORITY last (the default), low or high.
  --glide CH=MS   glide each pitch change of mono channel CH over MS ms,
                  1 to 10000 (Mono)
  --unison CH=N:CENTS
      Play each note of channel CH on N voices, 2 to 8, spread over CENTS
      cents, 0 to 1200 with at most two decimals (Unison).
  --kit CH=KEY:RECIPE[:HZ] [ARG=DELTA ...]
      Play key KEY (0 to 127) of channel CH with a recipe of its own, at
      HZ or the key's 12-TET pitch; an entry without CH= is on the
      channel of the entry before it in the same --kit (Kits and chokes).
  --choke CH=KEY+KEY[+KEY...]
      A choke group of channel CH's keys (Kits and chokes).
  --random CH=SEED:CENTS[:VEL]
      Detune each note of channel CH within plus or minus CENTS, 0 to
      1200 with at most two decimals, and move its velocity within plus
      or minus VEL, 0 to 126, from xorshift32 started at SEED, 1 to
      4294967295 (Random).
  --scala FILE.scl[:FILE.kbm]
      The tuning table from a Scala scale and keyboard mapping (Scala),
      in place of 12-TET; not with --tuning.
  --scale CH=NAME|MASK:ROOT
      Snap channel CH's pitches down to a scale before tuning (Scale).
      NAME is a tables.txt preset, MASK a decimal 0 to 4095; either may
      have :ROOT (0 to 11, default 0). Repeat or separate entries by commas.
  --arp CH=MODE:RATE[:OCT[:GATE]]
      Play the held keys on the file's tick grid (Arp and chord). MODE up,
      down, updown, downup, played, chord or random; RATE 1/1, 1/2, 1/4,
      1/8, 1/16 or 1/32, optionally followed by d (dotted) or t (triplet).
      OCT 1 to 11 (default 1); GATE a positive decimal or rational
      duration in steps (default 0.8; overlap above 1 is retained).
  --arp-latch CH[,CH...]
      Keep the final held set after release on an --arp channel.
  --arp-seed CH=UINT32
      Seed an --arp channel's random mode, 0 to 4294967295 (default 0).
  --arp-cond CH=STEP:COND
      Set one zero-based arp step's pre, not-pre or nei condition.
  --arp-nei CH=SOURCE_CH
      Read played steps of an earlier-numbered mapped arp channel.
  --chord CH=SHAPE[:STRUM_MS]
      Play each note as offsets rising from 0, joined by + (1 to 8 notes,
      for example 0+4+7). STRUM_MS is the upward total spread, 0 to 200
      (default 0), exact to whole nanoseconds; all notes end together.
  --rpn           take each channel's bend range from RPN 0 (RPN 0)
  --ctl CH=SOURCE:N | CH=default
      Write channel CH's controllers, pressure, release velocity or a
      per-note random value as ctl events to input N, 0 to 7, of its
      voices (Controllers); an entry without CH= is on the channel of the
      entry before it in the same --ctl.
  --mpe lower:N | upper:N
      Read an MPE zone (MPE, below): lower, master channel 1 with members
      2 to N + 1; upper, master channel 16 with members 15 down to 16 - N;
      N 1 to 15. Both zones may be given, in one --mpe or two.
  --fs HZ         the record's sample rate, 48000 (the default) or 44100
  --name NAME     the piece's name (default: the file's name, each run of
                  characters other than letters, digits, _ . - made one -)
  --out DIR       where session.tl and the stream files go (default
                  out/music/midi/NAME); a directory in music,
                  however its path spells it (a case variant, a link),
                  must be tools/music/NAME, where play.sh finds it, and a
                  session.tl that this tool did not write is never replaced
  --dry-run       read and convert, print the counts, write nothing
  --selftest      run the built-in checks (they render only when the
                  harness is built) and exit

Reading. Formats 0 and 1 are read, every MTrk chunk of them; format 2 is
refused, and other chunks are skipped and counted. The division is ticks a
quarter note, or SMPTE: 24, 25, 29 (29.97, that is 30000/1001) or 30 frames
a second times ticks a frame. A data byte where a status byte belongs
repeats the last channel status (running status); it is kept across meta
and SysEx events, which the standard says cancel it, and each use there is
counted. SysEx events (F0) and escapes (F7) are skipped and counted, as are
meta events other than tempo (51) and end of track (2F), and the bytes
after an end of track; a track without one ends at its last event. A
note-on at velocity 0 is a note-off. The file is refused on a status byte
F1 to FE in a track, a variable-length number longer than 4 bytes, an
event or chunk that runs past its end, a data byte above 7F, or a tempo of
0 or not 3 bytes long.

Time. Events of all tracks merge in order of tick, then track, then place
in the track. With ticks a quarter note, the tempo events of every track
make one tempo map (500000 us a quarter note before the first; of several
at one tick the last wins), and a tick's time is the sum over its tempo
segments of ticks times microseconds times 1000 over the division, kept as
an exact integer ratio and rounded half up to the ns: integers only, no
floating point. With SMPTE, a tick is 10^9 / (frames a second times ticks a
frame) ns, rounded the same way, and tempo events change nothing.

Channels, streams and patches. A channel plays only when --map (or --kit)
names it; a channel without one is skipped and its notes counted, channel 10
(percussion) among them. Each mapped channel that plays a note is one
stream, and its notes play the --map recipe as patch chCH; a mapped
channel without a note makes no stream, and the counts say "mapped, no
notes". With --program, a program change on a mapped channel other than 10
to a listed program makes the notes after it play that recipe as patch
chCHpPROG on the same stream; one to a program not listed brings back
patch chCH; on channel 10 it is counted and ignored. --filter puts its
filter on every patch of the channel. A stream is the file STREAM.tl, and
a case-insensitive file system (macOS's default) takes names that differ
only in case for one file, so stream names are compared case-folded: two
that differ only in case are refused before anything is written, as is
session in any case.

Notes. A note-off ends the oldest sounding note of its key on its channel
(first on, first off); one with no note to end is counted. CC 64 at 64 or
above holds the pedal down: a note-off then waits until it comes up, and a
key struck again while its pedalled note sounds ends that note at the new
note-on (the new note takes a voice of its own). A note still on at the end
of the file ends there. A note shorter than 1 ms is lengthened to 1 ms.

Arp and chord. These opt-in transforms replace the paired notes after
pedal holds, re-strikes, source-key chokes and the 1 ms minimum duration,
before tuning, layers, randomization and unison. They share compose.py's
held selector and chord spread law but use this tool's Voices allocation,
including stealing. Notes are ordinary mono events, with no placement
. Without the options, earlier conversions stay unchanged.

An arp step is 4 * division * RATE ticks, starting at tick 0 and continuing
while its onset is before the file's end tick. Noninteger tick steps and
SMPTE arps are refused. Dotted rates multiply by 3/2; triplets by 2/3.
Both the onset and the exact rational gate endpoint pass through the tempo
map, rounded half up to ns. Tempo changes never restart the grid. A gate
rounding to zero ns is refused; positive gates shorter than 1 ms use the
existing minimum duration. Gates may overlap and extend beyond the file's
end; the output tail follows the final off. Existing native length/voice
limits still refuse the conversion before output is written.

At each arp step all releases at a time precede all presses at that time.
Keys held on <= time < off are eligible; the velocity and patch belong to
the selected original press. Latch retains the complete set just before
the final simultaneous release, not previously released keys. A new press
starts a new set. Duplicate pitches keep their press identities. Ordering
and octave behavior match Piece.arp: chord mode plays the entire current
set at one octave, advancing its octave by step. Random mode draws once
per step, including empty rests, from compose.Rng(mix32(SEED)), independent
of --random and --ctl rand. Without latch an empty set is a rest.

An arp onset follows all original channel controls at the same ns, then
uses step and chord order for ties. A strummed chord copy keeps its source
strike order at its later onset. Bends, RPN, filter CC, channel pressure
and mapped CCs use the normal voice path; poly pressure still addresses
the original physical key across generated pitches and copies. A later
program change affects future physical presses, not keys already held or
latched. Chord copies keep the physical key-up velocity; --arp with
--ctl relvel is refused because its gates have no physical key-up byte.

--arp-cond CH=STEP:COND assigns pre, not-pre or nei to a zero-based grid
step; omitted steps are unconditional. Repeat or separate entries by commas.
One file is one pass, PRE initially false. Only conditional steps update PRE:
an empty or expansion-filtered step records false. --arp-nei CH=SOURCE_CH
names a lower-numbered mapped arp channel with the same integer tick step;
nei needs that explicit source. A step plays if any expanded note survives
its condition, before allocation/steals. Conditions retain all selection and
strike-random draws and act on all same-step layers/unison copies together.
Duplicate/out-of-range indices and self/forward/missing/non-arp/mismatched
neighbor sources refuse. The summary counts rejected generated strikes once,
not their expanded layer/unison copies. These are host filters, with no new
timeline rows.

A chord of n notes spreads offset k by (2*k*S + n-1) // (2*(n-1)) ns,
where S is the total spread; n=1 starts at the original onset. Its common
off is the paired source off. A spread reaching that off is refused by
channel, source tick and key. Chords also support SMPTE files. Chokes
change source-key lifetimes only, never run again on generated copies. Strike
randomness is grouped by (actual onset_ns, source strike order): separated
strummed onsets keep their own draw pair, while zero-spread tones, layers
and unison copies of the same onset share one. Earlier conversions have
one onset per source strike, so their draw order and values stay unchanged.

Duplicate or unmapped option channels, latch/seed without --arp, generated
keys outside 0..127, and invalid fields are refused. --arp and --chord on
one channel, or either with --scale, --kit, --mono or an MPE zone on that
channel, are refused by name. Other channels retain their existing modes.
The regression fixture and completion remain held on placement.

Voices. Each patch has up to --voices voices, and the piece declares only
as many as its notes use. The count is per patch, where  says per
stream, as a timeline voice plays one patch: a provisional reading for the
owner. A note-on takes, in this order: a voice whose last note's release
is over, the one free longest; a new voice, while the patch has fewer than
--voices; the voice whose note was released first (a release steal); the
voice whose note was struck first, of notes struck at one ns the first in
the file (a steal), whose note then ends with an off at the new note's
time, written before the new note-on. Other ties go to the lower voice. A
chord of more notes than --voices thus keeps the notes struck last. A note
stolen at the ns it was struck keeps its on, and the steal's off, in the
record; they change nothing, as the later note-on at that ns sets pitch
and velocity anew and keeps the attack the first began from the same level
. A release lasts the patch's carrier release_ms, the longest of
them for a routed recipe of several carriers (provisional). The counts say
how many of each. A pitch or filter event written for a voice at the ns a
note-on takes it is dropped: the record would apply it after that note-on,
to the new note.

Static pool. --pool N (1..64) divides one total voice budget before
translation. --reserve PATCH=R and --limit PATCH=L require --pool, accept
0..64, and may repeat or use commas. PATCH is the emitted name chCH,
chCHzN, chCHpPROG or chCHkKEY, not a recipe alias; duplicate/unknown names
refuse. Default reserve is0, limit --voices; mono defaults to1 and refuses
a larger limit. Demand is peak overlap of transformed and conditioned
notes including carrier release, with ends before starts at a tie.
Start each cap at its reserve (R>L or total R>N refuses); distribute the
rest round robin up to min(demand,limit). Order follows the original
--map entries, each plain map followed by --program declaration order,
then kits in channel/key order. An unused patch's reserve still consumes
budget. A zero cap skips copies, reported as "pool voice notes skipped";
the note count decreases only if all copies of a strike disappear.
Positive caps keep the old steal rules, including partial unison steals.
The split is static, with no later redistribution. --pool with MPE is
held by .

Pitch and velocity. A note's frequency is 12-TET with A4 at 440000 mHz,
through compose.py's mhz_m, or its key's --tuning entry; a bend of B
semitones makes it key + B in 12-TET, or the entry times 2^(B/12), rounded
half up to the mHz in 50-digit decimal and kept within 0 to 4294967295
(the counts say how often that bites). The velocity is vel * 32767 // 127.

Pitch bend. A bend's value v gives (v - 8192) / 8192 times --bend
semitones, and each note struck after it starts there. A pitch event
moves each voice of the channel still sounding, held or in its release,
whose frequency changes. Bends closer together than 1 ms merge: a run of
bends each less than 1 ms after the run's first writes only its last, at
its own time.

CC 74. On a channel with --filter, value v sets the cutoff to HZ times
2^((v - 64)/16), rounded to the mHz: 64 leaves the patch's cutoff, 0 is
four octaves below it and 127 nearly four above. A filter event moves each
sounding voice of the channel whose cutoff changes; a note-on resets its
voice to the patch's cutoff, as the engine does, and gets a filter event at
once when the value is not 64. Moves closer than 1 ms merge as bends do.
Without --filter, CC 74 is counted with the other controllers.

Mono. --mono CH=MODE[:PRIORITY] gives each patch of channel CH one voice.
The converter takes the channel's note-ons and note-offs in time order
(file order at one ns), with the pedal's holds and re-strikes as Notes
says, and keeps the notes whose key is down or held. After the events at
one ns, the note to sound is the priority's choice among them: last, the
one struck last; low or high, the lowest or the highest key, and of equal
keys the one struck last. When the choice changes: none left writes an
off; a note when none sounded, or when every key came up during that ns,
writes a note-on; in retrigger mode every other change writes a note-on
too (a retrigger, ); in legato mode it writes a pitch event  to
the new note's pitch, its velocity unused, and no note-on. A program
change between notes makes the next change a note-on on the new patch's
voice, after an off of the old one. The counts give the note-ons, the
retriggers among them (note-ons to a voice still sounding, held or in its
release) and the legato moves. Bends, CC 74 and RPN 0 apply as on any
channel. Not with --unison, a key range or a kit on the same channel.

--glide CH=MS makes each legato move, and each retrigger note-on to a
voice still sounding, a glide in equal cents: the retrigger's note-on
starts at the voice's sounding pitch, and n = ceil(MS / 5) pitch events
follow, the i-th at t + i * MS / n ms rounded half up to the ns, at f0 *
(f1 / f0)^(i / n) in 50-digit decimal, rounded half up to the mHz
(, as bends are), the last exactly on f1. A later move, note-on or kept bend that changes
the glide's target drops the steps not yet written; an unchanged target
keeps its glide. A changed bend moves the voice to its target, bent. A
note-on after every key came up, in legato mode, does not glide. CC 5
(portamento time) and CC 65 (portamento on or off) are counted with the
other controllers and ignored: --glide alone sets the glide.

Unison. --unison CH=N:CENTS plays each note of channel CH on N voices of
its patch, copy i (0 to N - 1) detuned by -CENTS/2 + i * CENTS/(N - 1)
cents, exact, its frequency rounded once to the mHz as a bend's is. Each
copy is a note to the rules of Voices, so a note takes N voices of
--voices, and N may not pass --voices; a steal takes one voice at a time,
so a stolen note can lose some of its copies before the rest. The copies
sound at one place: a mono record has no stereo spread. MIDI spread waits
for a MIDI piece that needs it ; WIDTH_MM versus SPREAD remains
unresolved. These options add no placement .

Layers and splits. A --map entry CH=RECIPE[:STREAM]@LO-HI plays only keys
LO to HI (0 to 127) of channel CH, as patch chCHzN, N its place from 1
among the channel's ranges in the order given. Its stream is STREAM, or
else the stream of the channel's entry without @, or else chCH. A channel
may have one entry without @ (it plays every key, with its program
changes, as before) and any number of ranges: disjoint ranges split the
keyboard, overlapping ones layer it, each a note on its own patch. On a
channel with no entry without @, a note in no range is skipped and
counted, and program changes are counted and ignored. A channel's patches
may share a stream; two channels may not.

Append /VLO-VHI to a key range for inclusive velocities1..127. Selection
uses the velocity after --random, before unison and PRE/NEI, preserving
every random draw. A strike with no surviving layer reports
"velocity-zone strikes skipped"; a key-only miss keeps its old count.
Unmarked overlaps layer at full level. Omitting / retains the old events.

Mark both overlapping entries with ~ to crossfade. Their velocity bounds
must be explicit (/VLO-VHI is required with ~) and
must be strictly staggered; equal/nested ranges, one-sided marks and
three simultaneous zones involving a mark refuse by name. A marked
entry without an overlap writes zero attenuation. For overlap L..H and
n=H-L+1, t=(velocity-L+1)/(n+1); upper/lower attenuation is -100*log10(t)
and -100*log10(1-t), in dB10, Decimal50 rounded half up and clamped0..1200.
A one-velocity overlap gives30/30; seven velocities give upper/lower
90/6,60/12,43/20,30/30,20/43,12/60,6/90. This approximates equal power
after quantization. vel_sens_q8 is not used: changing velocity also changes
the FM index; a velocity-sourced slot cannot represent this logarithmic
overlap rules.

Each carrier takes a free mod slot, ctlN levelK1200 with shift0, sharing
the lowest free ctl input. Existing slots/ctl sources, even amount0,
macro ownership and every --ctl destination reserve their places.
Exhaustion refuses by patch name. Before every on, after old controllers,
write ctl N ceil(a*32768/1200), even repeated values and zero outside
overlaps: the native integer contribution is exactly a. Shape (even
drive0), filter/cascade/FENV, formant, --filter, and amp  refuse.
Nonzero carrier-level routes, active carrier LFO level modulation and
nonzero level/both key-scaling slopes also refuse. Other oscillator,
WT/PD/KS, feedback, pitch/index/PEG and PM/AM/ring/sync paths may remain.
Every actual written carrier level B, on every leveller pass and final
write, must satisfy B+a_max<=1200 and B>=0,1,36,61 for1,2,3,4 summed
carriers; convex vector mixing needs B>=0. An individual carrier's
common-shift clamp refuses. The leveller is unchanged. Crossfade records
are rendered/replayed privately and published only after validation, so
a late bound refusal preserves the destination. Temporary WAVs are
removed. Zones/crossfades/pools keep mono placement unchanged (/).

Kits and chokes. --kit plays each listed key of channel CH as patch
chCHkKEY on stream chCH, at HZ rounded half up to the mHz, or at the key's
12-TET pitch (compose.py's mhz_m, A4 at 440000 mHz) when HZ is left out.
Bends, --tuning and --scala leave a kit's pitches alone; --random's cents
still apply. A key with no entry is skipped and counted. A kit channel has
no --map. --choke CH=KEY+KEY... makes a group: a note-on of one of its
keys ends, at its ns, the channel's notes of the group's other keys whose
key is down or held by the pedal, the off written before the on; a note
already released keeps its release. The choked note's own note-off then
ends nothing.

Random. --random CH=SEED:CENTS[:VEL] draws from xorshift32 (Marsaglia's: x
^= x << 13, x ^= x >> 17, x ^= x << 5, in 32 bits) started at SEED, two
draws a note, for the channel's notes in the order they strike (time, then
file order); layers and unison copies share their note's draws, and a note
that mono hides or a kit, key/velocity range or .kbm skips still draws. The first draw x gives the detune, x mod (200
* CENTS + 1) - 100 * CENTS hundredths of a cent; the second the velocity
offset, x mod (2 * VEL + 1) - VEL, the MIDI velocity then kept within 1 to
127. VEL defaults to 0.

Scale. --scale CH=NAME|MASK:ROOT uses compose.py's scale_mask and scale_lock:
the largest allowed key at or below the played key, mask 0 off, root 0
when omitted. Names come from tables.txt; numeric masks are decimal.
Pair note-ons and offs on the file's key, then lock the sounding key
before --tuning, --scala's .kbm lookup and bends. Two keys that lock to
one pitch keep their own voices and offs. Key ranges, stealing, reuse,
chokes, pedal and poly pressure still use the file's key; --random takes
the same draws in the same order. A layer or unison copy keeps the locked
pitch. Refused by name: an unknown preset, mask outside 0 to 4095, root
outside 0 to 11, a repeated or unmapped channel, a kit or mono channel,
any master or member of an MPE zone, a .kbm map size other than 12, and
a key that would lock below 0. These refusals apply even to mask 0.
--scale changes pitch only, no placement: this translator writes mono
records ; later output placing notes by key uses the locked key.

Scala. --scala FILE.scl[:FILE.kbm] (the part after the last colon is the
.kbm when it ends in .kbm, in any case) builds the 128-entry table
--tuning reads. Both files are read as Latin-1, and a line starting with !
is a comment. The .scl: a description line, then the count of notes n (1
or more; blank lines are skipped from here on), then n pitches, the first
word of each: with a period, cents (a decimal, may be negative); without,
a ratio N or N/D above 0. The last pitch is the period, and degree d is d
div n periods times pitch d mod n (1/1 at 0). Without a .kbm, keys 0 to
127 take degree key - 60, and key 60 sounds at 440000 * 2^(-9/12) mHz,
12-TET's middle C (261625.5653... mHz). A .kbm gives the map size, first
and last key, middle key, reference key, its frequency in Hz, the formal
octave degree, then size entries, x for an unmapped key (entries left out
are unmapped): key k's degree is entry (k - middle) mod size plus (k -
middle) div size octaves, or k - middle when the size is 0. Key k gets the
reference's mHz times ratio(degree k) / ratio(degree of the reference), in
exact Fractions when every pitch used is a ratio and the frequency a
decimal, otherwise in Python's decimal module at 50 significant digits
(2^(c/1200) as exp(c * ln 2 / 1200)), rounded half up to the mHz. A key
outside first to last, mapped to x, or whose mHz falls outside 1 to
4294967295, is unmapped: its notes are skipped and counted (not on a kit
channel). Refused, naming the file and the line: a line that is not what
its place asks for, a count or pitch not above 0, fewer pitch lines than
the count, fewer than 7 .kbm lines before the mapping, or keys out of
order; naming the file: an unmapped reference key.

Controllers. --ctl CH=SOURCE:N writes a source of channel CH as ctl
events to input ctlN of the voices (the modulation matrix of modulation matrix, read by a
recipe's mod slots; ). Sources: ccN, CC N (1 to 119) as 7 bits;
ccN+, CC N (1 to 31) with CC N + 32 as its low 7 bits, an MSB clearing the
LSB; nrpnM.L, the NRPN that CC 99 (M) and CC 98 (L) select, its value from
data entry CC 6 (MSB, which clears the LSB) and CC 38, CC 101 or 100
deselecting it; pressure, channel pressure; polyat, polyphonic
aftertouch; relvel, the note-off velocity; rand:SEED, a random value from
its own xorshift32 started at SEED (1 to 4294967295), never --random's.
CH=default is 's assignment: ctl0 cc1 (the mod wheel), ctl1 pressure
and polyat, ctl2 cc2 (breath), ctl3 cc11 (expression), ctl5 relvel and
ctl6 rand, seeded by 2654435769 times CH modulo 2^32; ctl4 (MPE slide)
waits for --mpe and ctl7 is free. Values: 7 bits v give (v * 32768 + 63)
div 127, 14 bits v (v * 32768 + 8191) div 16383, both rounded half up, 0
to 32768; a draw x gives (x >> 16) - 32768. A channel message goes to the
channel's sounding voices (held or in release), poly aftertouch only to
the voices of its key; a voice is written only when the value differs
from the last written to that input of that voice. At a note-on, just
before its on, each input the patch reads gets the channel's value (0
until a message sets it; an input fed by pressure and polyat takes the
channel pressure's), so the latches poly aftertouch and relvel leave on a
reused voice go back; rand's input gets one draw a voice note-on (each
unison copy one, in the order the voices are taken), drawn whether read
or not. relvel is written at the note's off, just before it: at the
key-up, or with the key-up's byte at the pedal's release or the key's
re-strike; a note-on at velocity 0 counts as 64 (provisional); a steal, a
choke and a note still on at the end of the file write none. Moves of one
source (of one key for polyat) merge as bends do (1 ms). Only inputs that
a slot with a nonzero amount reads get events, so a recipe without slots
records nothing new. Refused: a channel not mapped or with --mono; CC 6,
38, 64 and 96 to 101; a source mapped twice, CCs claimed twice (ccN+
claims N and N + 32), CC 74 with --filter, nrpn127.127, two rands, and an
input shared with rand. Several other sources may feed one input; the last
message wins. Messages --ctl takes are not counted as ignored. --ctl
writes no pan or position field .

MPE. --mpe reads a zone of MIDI Polyphonic Expression (; the rules are
provisional, P row "Provisional choices of regression"): every note of a zone,
on its master channel or a member, plays the master's --map patch, one
pool and one patch a zone, and the master's --ctl messages reach every
voice of the zone. A member's pitch bend uses the member range, 48
semitones until RPN 0 on any member of the zone changes it for all of
them; the master's uses the master range, --bend (2 by default) until RPN
0 on the master changes it. RPN 0 is read on a zone's channels without
--rpn. A note's pitch is its key plus the master's bend plus its member's,
each (v - 8192) * range / 8192 semitones as an exact fraction, summed
exactly and rounded once, half up, to the mHz. A member's channel pressure
goes to ctl1 and its CC 74 (slide) to ctl4 of its note's voice, as --ctl
writes them (7 bits, merged at 1 ms, a voice written only when the value
changes); at a note-on, ctl1 and ctl4 get the member's last values (0
until a message sets them). Under --mpe, CC 74 is never the filter event:
a member's is slide, a master's is counted (or --ctl's). Reuse: a
member's bend, pressure and slide move its latest note, held or in its
release; a note-on on the member makes the new note the latest, and the
earlier note keeps its member bend and ctl values as last written (the
master's bend still moves it). The pedal (CC 64 on any channel of a zone)
holds the whole zone; a re-strike under it ends only a held note of the
same key on the same channel. Refused: a zone whose master has no plain
--map entry, or has a key range, --mono, --unison, --choke or --random; a
member with its own --map or --kit; --ctl on a master mapping ctl1 or
ctl4 (so not CH=default); a zone given twice; zones that overlap (the
lower and upper members together come to 14 at most). An MPE
configuration message (RPN 6 data entry) is counted and ignored, the
zones coming from the command line. Program changes on a zone's channels
are counted and ignored, poly aftertouch and other controllers of a
member counted. --mpe and --fs write no pan or position field .

RPN 0. With --rpn, CC 101 and CC 100 select a registered parameter for
their channel (127 and 127, the null parameter, deselects; CC 99 or 98, an
NRPN, deselects too and is counted), and while RPN 0 is selected, CC 6
sets the channel's bend range in semitones (and its cents to 0) and CC 38
its cents (values 100 to 127 clamp to 99), in place of --bend for the bends after it; a voice already bent
keeps its pitch until the next bend. CC 96 and 97 are counted and ignored.
Without --rpn these controllers are counted and ignored, as before. On a
channel whose --ctl maps an NRPN, CC 98 to 101 are --ctl's (Controllers).

Counted and ignored: every other controller (CC 1 modulation, 7 volume, 10
pan, 11 expression, 120 all sound off, 121 reset, 123 all notes off among
them), polyphonic aftertouch, channel pressure (each unless --ctl takes
it), program changes without --program, SysEx and meta events.

Output. A compose.py Piece: session.tl (the header, patches, filters and
voices) and one STREAM.tl per stream in --out, then compose.py's finish:
every carrier level moved by one amount until the peak sits within 0.5 dB
of -1.5 dBFS with no clipped sample, the hash written into session.tl,
NAME-mono.wav and NAME-mono.tl rendered into out/music/midi, and the
merged record replayed to the same hash. The leveller renders at most 8
times and lowers the levels at most 1.5 dB between two renders, 10.5 dB in
all, and no level goes above 0 dB; a file it cannot level stops with
"levels did not settle", and the message says which way it failed. Too
loud: give the patches quieter carriers, --map 'CH=RECIPE c_level=N' (N dB
quieter), and the same in each --program entry. Too quiet (soft notes, few
sounding at once): this tool has no option to raise them yet. length_ns is
the later of the
last event and the end of the file, plus the tail. The tool prints the
counts, the record's directory, the PLAY line and the hash. A record
outside music says that it is an unpinned conversion, not a
committed record.

Not done: the held regression/regression fixtures ; format 2; CC 120, 121 and 123 as the messages they are; CC 1 as
vibrato; CC 5 and 65 as portamento
(--glide sets it); --mono with --unison, key ranges or a kit; a unison's
stereo spread ; --ctl on a mono channel, CC 96
and 97 stepping an NRPN, and slots fed through another input (regression);
live input; MPE configuration messages read from the file, and
--mpe with --mono. An app's live voice
manager may choose otherwise than these rules, and its sound then differs
from this record.
"""
import argparse
import bisect
import contextlib
import decimal
import hashlib
import heapq
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import textwrap
import traceback
from collections import Counter, deque
from fractions import Fraction

sys.dont_write_bytecode = True                      # importing compose.py leaves no __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose  # noqa: E402  the composer beside this file: recipes, Piece, finish

ROOT = compose.SPIKE               # the repository root
RENDERS = os.path.join(compose.OUT, 'midi')         # default records, and every WAV and merged record
MARK = 'tools/music/midi2tl.py'                     # a session.tl that names this may be written over
# The option patterns match the whole word, in ASCII: \Z, as $ also matches before a final
# newline, and [0-9], as \d also matches the digits of other scripts.
NAME_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,30}\Z')  # a harness name, and a safe file name
NUMBER_RE = re.compile(r'[0-9]+(\.[0-9]+)?\Z')
MAP_RE = re.compile(r'([0-9]+)=([A-Za-z0-9_.-]+)(?::(.+))?\Z')
PROGRAM_RE = re.compile(r'([0-9]+)=([A-Za-z0-9_.-]+)\Z')
FILTER_RE = re.compile(r'([0-9]+)=(lp|bp|hp|notch):([0-9]+(?:\.[0-9]+)?):([0-9]+(?:\.[0-9]+)?)\Z')
ZONE_RE = re.compile(r'(.*)@([0-9]+)-([0-9]+)(?:/([0-9]+)-([0-9]+))?(~?)\Z')
MONO_RE = re.compile(r'([0-9]+)=(retrigger|legato)(?::(last|low|high))?\Z')
GLIDE_RE = re.compile(r'([0-9]+)=([0-9]+)\Z')
UNISON_RE = re.compile(r'([0-9]+)=([0-9]+):([0-9]+(?:\.[0-9]{1,2})?)\Z')
KIT_RE = re.compile(r'(?:([0-9]+)=)?([0-9]+):([A-Za-z0-9_.-]+)(?::([0-9]+(?:\.[0-9]+)?))?\Z')
CHOKE_RE = re.compile(r'([0-9]+)=([0-9]+(?:\+[0-9]+)+)\Z')
RANDOM_RE = re.compile(r'([0-9]+)=([0-9]+):([0-9]+(?:\.[0-9]{1,2})?)(?::([0-9]+))?\Z')
CTL_RE = re.compile(r'(?:([0-9]+)=)?(.+)\Z')
CTL_SRC_RE = re.compile(r'(?:cc([0-9]{1,3})(\+?)|nrpn([0-9]{1,3})\.([0-9]{1,3})|(pressure|polyat|relvel)'
                        r'|rand:([0-9]{1,10}))\Z')
MPE_RE = re.compile(r'(lower|upper):([0-9]{1,2})\Z')
CTL_REFUSED = (6, 38, 64, 96, 97, 98, 99, 100, 101)   # --ctl: data entry, the pedal, the RPN and NRPN selects
GLIDE_STEP_MS = 5                # a glide's pitch events come about this far apart (Mono)
GLIDE_MAX_MS = 10000
SCALA_PREC = 50                  # significant digits of the decimal arithmetic of --scala
LAST = 1 << 62                   # the file order of an off at the end of the file, after every event
DEFAULT_US = 500000              # microseconds a quarter note before the first tempo event
MERGE_NS = 1000000               # bends, CC 74 and --ctl moves closer than this merge into the last
MIN_NOTE_NS = 1000000            # a shorter note is lengthened to this
ROLE_MAX = 600                   # a longer role names a stream's patches alone (a line holds 1022 bytes)
SMPTE_FPS = {24: (24, 1), 25: (25, 1), 29: (30000, 1001), 30: (30, 1)}   # frames a second, num/den
DATA_BYTES = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}
PERCUSSION = 9                   # MIDI channel 10, counted from 0
FS = 48000                       # compose.Piece's rate
TIMES_NOTE = "# Times: the MIDI file's ticks through its tempo map, exact, rounded half up to the ns.\n"
SESSION_NOTE = '# An unpinned conversion by %s, not a committed record.\n' % MARK


# ---------------------------------------------------------------------------
# Reading a Standard MIDI File
class MidiError(Exception):
    """A file this tool cannot read as a Standard MIDI File of format 0 or 1."""


def read_vlq(d, i, end, where):
    """The variable-length number at d[i], at most 4 bytes, as (value, index after it)."""
    v = 0
    for _ in range(4):
        if i >= end:
            raise MidiError('%s: a number runs past the end of its chunk' % where)
        b = d[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if b < 0x80:
            return v, i
    raise MidiError('%s: a variable-length number longer than 4 bytes' % where)


def read_track(d, i, end, trk, info, events, tempos):
    """Track trk, the MTrk body d[i:end]: its channel events appended to events as (tick, trk,
    index, kind, channel, data1, data2), its tempos to tempos as (tick, trk, index, us), and what
    it skips counted in info."""
    tick = idx = 0
    status, crossed = None, False      # running status, and whether a meta or SysEx event came since
    while i < end:
        where = 'track %d, byte %d' % (trk, i)
        delta, i = read_vlq(d, i, end, where)
        tick += delta
        if i >= end:
            raise MidiError('%s: an event runs past the end of its chunk' % where)
        b = d[i]
        if b == 0xFF:                                   # a meta event
            if i + 1 >= end:
                raise MidiError('%s: a meta event runs past the end of its chunk' % where)
            kind = d[i + 1]
            n, i = read_vlq(d, i + 2, end, where)
            if i + n > end:
                raise MidiError('%s: a meta event runs past the end of its chunk' % where)
            body, i = d[i:i + n], i + n
            crossed = status is not None
            if kind == 0x2F:
                info['after_end'] += end - i
                break
            if kind == 0x51:
                if n != 3:
                    raise MidiError('%s: a tempo event of %d bytes, not 3' % (where, n))
                us = (body[0] << 16) | (body[1] << 8) | body[2]
                if us == 0:
                    raise MidiError('%s: a tempo of 0 microseconds a quarter note' % where)
                tempos.append((tick, trk, idx, us))
            else:
                info['meta'][kind] += 1
        elif b in (0xF0, 0xF7):                         # SysEx, or an escape: skipped
            n, i = read_vlq(d, i + 1, end, where)
            if i + n > end:
                raise MidiError('%s: a SysEx event runs past the end of its chunk' % where)
            i += n
            info['sysex'] += 1
            crossed = status is not None
        else:
            if b > 0xEF:
                raise MidiError('%s: status byte 0x%02X does not belong in a track' % (where, b))
            if b & 0x80:
                status, crossed = b, False
                i += 1
            elif status is None:
                raise MidiError('%s: data byte 0x%02X with no running status' % (where, b))
            elif crossed:
                info['crossed'] += 1
                crossed = False
            n = DATA_BYTES[status & 0xF0]
            if i + n > end:
                raise MidiError('%s: a channel event runs past the end of its chunk' % where)
            data, i = d[i:i + n], i + n
            if max(data) > 0x7F:
                raise MidiError('%s: a data byte above 0x7F' % where)
            events.append((tick, trk, idx, status & 0xF0, status & 0x0F, data[0], data[1] if n == 2 else 0))
        idx += 1
    else:
        info['no_end'] += 1
    info['end_tick'] = max(info['end_tick'], tick)


def parse_smf(data):
    """(info, events, tempos) of a Standard MIDI File's bytes, format 0 or 1."""
    d = bytes(data)
    if len(d) < 14 or d[:4] != b'MThd':
        raise MidiError('not a Standard MIDI File: no MThd header')
    hlen = struct.unpack('>I', d[4:8])[0]
    if hlen < 6 or 8 + hlen > len(d):
        raise MidiError('an MThd header of %d bytes' % hlen)
    fmt, ntrk, div = struct.unpack('>HHH', d[8:14])
    if fmt == 2:
        raise MidiError('format 2 (independent sequences) is not read')
    if fmt > 2:
        raise MidiError('format %d is not a Standard MIDI File format' % fmt)
    info = {'format': fmt, 'header_tracks': ntrk, 'tracks': 0, 'end_tick': 0, 'meta': Counter(),
            'sysex': 0, 'crossed': 0, 'no_end': 0, 'after_end': 0, 'other_chunks': 0, 'trailing': 0}
    if div & 0x8000:
        fps, tpf = 256 - (div >> 8), div & 0xFF
        if fps not in SMPTE_FPS or tpf == 0:
            raise MidiError('an SMPTE division of %d frames a second and %d ticks a frame' % (fps, tpf))
        info['smpte'] = (fps, tpf)
    elif div == 0:
        raise MidiError('a division of 0 ticks a quarter note')
    else:
        info['tpqn'] = div
    events, tempos = [], []
    i = 8 + hlen
    while i + 8 <= len(d):
        tag, n = d[i:i + 4], struct.unpack('>I', d[i + 4:i + 8])[0]
        if i + 8 + n > len(d):
            raise MidiError('the chunk %r at byte %d runs past the end of the file' % (tag.decode('latin-1'), i))
        if tag == b'MTrk':
            read_track(d, i + 8, i + 8 + n, info['tracks'], info, events, tempos)
            info['tracks'] += 1
        else:
            info['other_chunks'] += 1
        i += 8 + n
    info['trailing'] = len(d) - i
    if not info['tracks']:
        raise MidiError('no MTrk chunk')
    return info, events, tempos


class TempoMap:
    """MIDI ticks to ns, exactly: integers only, each time rounded half up to the ns."""

    def __init__(self, info, tempos):
        self.smpte = None
        if 'smpte' in info:
            fps, tpf = info['smpte']
            num, den = SMPTE_FPS[fps]
            self.smpte = (10 ** 9 * den, num * tpf)       # a tick is a / b ns
            return
        self.div = info['tpqn']
        self.ticks, self.us, self.base = [0], [DEFAULT_US], [0]   # base: ns times div at each start
        for tick, _, _, us in sorted(tempos):
            if tick != self.ticks[-1]:
                self.base.append(self.base[-1] + (tick - self.ticks[-1]) * self.us[-1] * 1000)
                self.ticks.append(tick)
                self.us.append(us)
            else:
                self.us[-1] = us                          # of several at one tick the last wins

    def ns(self, tick):
        if self.smpte:
            a, b = self.smpte
            return (2 * tick * a + b) // (2 * b)
        k = bisect.bisect_right(self.ticks, tick) - 1
        num = self.base[k] + (tick - self.ticks[k]) * self.us[k] * 1000
        return (2 * num + self.div) // (2 * self.div)


def merged(times):
    """The indices kept of sorted times when moves closer than MERGE_NS merge: a run of moves
    each less than MERGE_NS after the run's first keeps only its last, at its own time."""
    keep, k = [], 0
    while k < len(times):
        j = k
        while j + 1 < len(times) and times[j + 1] < times[k] + MERGE_NS:
            j += 1
        keep.append(j)
        k = j + 1
    return keep


def note_mhz(key, semis, tuning=None):
    """Millihertz of MIDI key bent by semis semitones: 12-TET with A4 at 440000 mHz through
    compose.mhz_m, or the --tuning entry times 2^(semis/12); not yet kept within 32 bits."""
    if tuning is None:
        return compose.mhz_m(key + semis)
    return pitch_mhz(tuning[key], semis, 12)


def pitch_mhz(value, steps, divisions):
    """Scale an integer mHz pitch and round half up; keep the Fraction input exact."""
    return int(compose.pitch_scale(value, steps, divisions).to_integral_value(rounding=decimal.ROUND_HALF_UP))


def glide_mhz(f0, f1, i, n):
    """One geometric glide step, in fixed-precision decimal, half up to mHz."""
    if i == n or f0 <= 0:
        return f1
    with decimal.localcontext(decimal.Context(prec=compose.NUMERIC_PREC, rounding=decimal.ROUND_HALF_EVEN)):
        ratio = decimal.Decimal(f1) / f0
        value = decimal.Decimal(f0) * (ratio.ln() * i / n).exp()
        return int(value.to_integral_value(rounding=decimal.ROUND_HALF_UP))


def crossfade_attenuation(velocity, lo, hi, upper):
    """: quantized equal power, in raw dB10, at an inclusive overlap."""
    if not lo <= velocity <= hi:
        raise ValueError('crossfade velocity is outside its overlap')
    with decimal.localcontext(decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN)):
        t = decimal.Decimal(velocity - lo + 1) / (hi - lo + 2)
        if not upper:
            t = 1 - t
        return min(1200, max(0, int((-100 * t.log10()).to_integral_value(
            rounding=decimal.ROUND_HALF_UP))))


def crossfade_ctl(attenuation):
    """Exact  conversion, shared with opt-in composer compensation."""
    return compose.carrier_attenuation_ctl(attenuation)


def crossfade_settings(name, operators, extra, reserved, fs):
    """Keep MIDI's  interface and diagnostics through the shared validator."""
    return compose.carrier_attenuation_settings(name, operators, extra, reserved, fs)


# ---------------------------------------------------------------------------
# Notes, voices and events
class Note:
    """A note: key, velocity, on and off in ns, its strike order (the place of its note-on
    among all the file's events, merged: it grows with each note struck, so of notes struck at
    one ns it says which came first), and its patch key (channel, program or None); patch is
    None when its channel is not mapped."""
    __slots__ = ('key', 'vel', 'on', 'off', 'order', 'patch', 'off_order', 'cents', 'fixed', 'up', 'mch', 'mb',
                 'locked_key', 'origin_key', 'tick', '_arp_step', '_xfade_a')

    def __init__(self, key, vel, on, order, patch):
        self.key, self.vel, self.on, self.off, self.order, self.patch = key, vel, on, None, order, patch
        self.off_order, self.cents, self.fixed = None, Fraction(0), None   # fixed: a kit entry's mHz
        self.up = None                                # the key-up's velocity byte (--ctl's relvel)
        self.mch, self.mb = None, Fraction(0)         # --mpe: the note's channel in a zone, its member bend
        self.locked_key = None                       # --scale: pitch only; key remains the file's identity
        self.origin_key, self.tick = key, None         # generated notes retain physical key and source tick
        self._arp_step = None                          # private logical grid provenance for conditions
        self._xfade_a = None                           # regression, assigned after random velocity

    def pitch_key(self):
        return self.key if self.locked_key is None else self.locked_key

    def copy(self, patch, **kw):
        """The same note on patch key patch, with the attributes kw changed."""
        n = Note(self.key, self.vel, self.on, self.order, patch)
        n.off, n.off_order, n.cents, n.fixed, n.up = self.off, self.off_order, self.cents, self.fixed, self.up
        n.mch, n.mb = self.mch, self.mb
        n.locked_key = self.locked_key
        n.origin_key, n.tick = self.origin_key, self.tick
        n._arp_step = self._arp_step
        n._xfade_a = self._xfade_a
        for k, v in kw.items():
            setattr(n, k, v)
        return n


def xorshift32(x):
    """Marsaglia's xorshift32 with shifts 13, 17 and 5: the next state of x (1 to 4294967295)."""
    x ^= (x << 13) & 0xFFFFFFFF
    x ^= x >> 17
    return x ^ ((x << 5) & 0xFFFFFFFF)


def q7(v):
    """A 7-bit controller value as a ctl value: (v * 32768 + 63) // 127, which
    rounds half up and never meets a tie; 0 gives 0 and 127 gives 32768."""
    return (v * 32768 + 63) // 127


def q14(v):
    """A 14-bit value (MSB * 128 + LSB) as a ctl value, rounded half up: 16383 gives 32768."""
    return (v * 32768 + 8191) // 16383


class Pool:
    """One patch's voices on its channel's stream, each [note, mHz, cutoff mHz]."""

    def __init__(self, name, rname, kw, voices, flt, crossfade=None, fs=FS):
        self.name, self.rname = name, rname
        self.car, self.mod, self.comment, *self.extra = compose.recipe(rname, **kw)
        ops = [self.car, self.mod] + (list(self.extra[0].get('more', [])) if self.extra else [])
        mask = self.extra[0].get('carriers', 1) if self.extra else 1    # a routed recipe's carriers
        self.rel_ms = max(o[8] for i, o in enumerate(ops) if mask >> i & 1)    # the longest carrier release
        self.rel = self.rel_ms * 1000000              # that release, ns
        self.max, self.filter = voices, flt           # filter: (mode, cutoff mHz, Q milli) or None
        self.slots, self.first = [], 0
        self.crossfade = crossfade
        self.crossfade_input = None
        if crossfade is not None:
            if flt:
                raise ValueError('crossfade patch %s: --filter is outside the proved carrier-level path' % name)
            extra, self.crossfade_input = crossfade_settings(name, ops, self.extra[0] if self.extra else {},
                                                              crossfade[1], fs)
            self.extra = [extra]
        mods = self.extra[0].get('mod', {}) if self.extra else {}   # the recipe's slots, checked at load
        self.reads = sorted({int(s[3:]) for s, _, amount, _ in mods.values() if s.startswith('ctl') and amount})
        self.latch = {}                               # --ctl: voice -> the 8 ctl values last written

    def pick(self, t, st):
        """The voice a note-on at t takes (the docstring's Voices), counting steals in st."""
        free = [(n.off + self.rel, k) for k, (n, _, _) in enumerate(self.slots) if n.off + self.rel <= t]
        if free:
            return min(free)[1]
        if len(self.slots) < self.max:
            self.slots.append([None, 0, None])
            return len(self.slots) - 1
        released = [(n.off, k) for k, (n, _, _) in enumerate(self.slots) if n.off <= t]
        if released:
            st['release_steals'] += 1
            return min(released)[1]
        st['steals'] += 1                             # struck first; at one ns, first in the file
        return min((n.on, n.order, k) for k, (n, _, _) in enumerate(self.slots))[2]


class Conversion:
    """A MIDI file read, its notes paired, and each mapped channel's notes, bends and CC 74
    moves turned into voices and events by the rules of the docstring."""

    def __init__(self, data, cfg):
        self.cfg = cfg
        self.info, events, tempos = parse_smf(data)
        self.info['tempo_events'] = len(tempos)
        self.tm = TempoMap(self.info, tempos)
        if cfg.arps and 'tpqn' not in self.info:
            raise ValueError('--arp requires a PPQN tick grid; SMPTE is not supported')
        for c, (_, rate, _, _, _) in cfg.arps.items():
            if (4 * self.info['tpqn'] * rate).denominator != 1:
                raise ValueError('--arp channel %d: RATE gives a noninteger tick step' % (c + 1))
            step = int(4 * self.info['tpqn'] * rate)
            count = (self.info['end_tick'] + step - 1) // step
            if any(g >= count for g in cfg.arp_conditions.get(c, {})):
                raise ValueError('--arp-cond channel %d: STEP is outside the file grid' % (c + 1))
            source = cfg.arp_neighbors.get(c)
            if source is not None and rate != cfg.arps[source][1]:
                raise ValueError('--arp-nei channel %d: source channel %d has a different tick step' % (c + 1, source + 1))
        self.arp_played = [set() for _ in range(16)]
        self.end_ns = self.tm.ns(self.info['end_tick'])
        self.st = [Counter() for _ in range(16)]       # per channel: what happened, counted
        self.ccs = [Counter() for _ in range(16)]      # per channel: other controllers, by number
        self.notes = [[] for _ in range(16)]
        self.bends = [[] for _ in range(16)]           # (t, order, value)
        self.cc74 = [[] for _ in range(16)]            # (t, order, value), on a channel with --filter
        self.ranges = [[] for _ in range(16)]          # (t, order, semitones), RPN 0 with --rpn
        self.pools = [{} for _ in range(16)]           # patch key -> Pool, in the order of first notes
        self.out = [[] for _ in range(16)]             # (t, kind, pool, voice in pool, value, label)
        self.moved = {}                                # (channel, patch, voice) -> (t, its moves at t in out)
        self.clamped = 0
        self.ctlmsgs = [[] for _ in range(16)]         # --ctl: (t, order, source, value, key or None, label)
        self.wide = [{} for _ in range(16)]            # --ctl: ccN+ N -> [MSB, LSB]
        self.nsel = [None] * 16                        # --ctl: the NRPN CC 99 and 98 select, or None
        self.ndata = [{} for _ in range(16)]           # --ctl: NRPN (M, L) -> [MSB, LSB]
        self.zacts = [[] for _ in range(16)]           # --mpe, by master: (t, order, what, channel, value)
        self.pair(sorted(events))
        self.pool_caps = {}
        self.pool_templates = {}
        if cfg.pool_total is not None:
            self.split_pool()
        for c in range(16):
            if cfg.mapped(c):
                self.allocate_mono(c) if c in cfg.monos else self.allocate(c)

    def pair(self, events):
        """Notes from note-ons and note-offs with the pedal's holds; programs, bends and CC 74
        moves taken; everything else counted."""
        cfg = self.cfg
        zone = cfg.zone_of                   # --mpe: each zone channel's master; empty without it
        patch = [(c, None) if cfg.mapped(c) else None for c in range(16)]
        held = [{} for _ in range(16)]       # key -> notes whose key is down, first on first
        kept = [[] for _ in range(16)]       # notes whose key is up while the pedal holds them
        pedal = [False] * 16
        rpn = [(127, 127)] * 16              # --rpn: the parameter selected by CC 101 and 100, or None
        bend = [(int(cfg.bend), int(cfg.bend * 100) % 100)] * 16   # --rpn: the range, semitones and cents
        for c in zone:
            if zone[c] != c:
                bend[c] = (48, 0)                # an MPE member's range
        for order, (tick, _, _, kind, c, a, b) in enumerate(events):
            t, st = self.tm.ns(tick), self.st[c]
            z = zone.get(c, c)                   # a zone's pedal and pedal-held notes are its master's
            if kind == 0x90 and b:
                mch = c if c in zone else None
                for n in [n for n in kept[z] if n.key == a and n.mch == mch]:
                    n.off, n.off_order = t, order        # its key struck again under the pedal
                    kept[z].remove(n)
                    st['restruck'] += 1
                if c in cfg.chokes and (c not in cfg.kits or a in cfg.kits[c]):
                    self.choke(c, a, t, order, held[c], kept[c])
                n = Note(a, b, t, order, (z, None) if c in zone else patch[c])
                n.mch, n.tick = mch, tick
                held[c].setdefault(a, deque()).append(n)
                self.notes[z].append(n)
                if z != c:
                    st['mpe_notes'] += 1
            elif kind in (0x80, 0x90):                   # a note-off, or a note-on at velocity 0
                q = held[c].get(a)
                if not q:
                    st['orphan_offs'] += 1
                elif q[0].off is not None:               # a note a choke ended: its off is written
                    q.popleft()
                elif pedal[z]:
                    n = q.popleft()
                    n.up = b if kind == 0x80 else 64     # the key-up's byte; velocity 0 counts as 64
                    kept[z].append(n)
                    st['pedal_held'] += 1
                else:
                    n = q.popleft()
                    n.off, n.off_order, n.up = t, order, b if kind == 0x80 else 64
            elif kind == 0xB0 and a == 64:
                if b >= 64 and not pedal[z]:
                    st['pedal_downs'] += 1
                elif b < 64 and pedal[z]:
                    for n in kept[z]:
                        n.off, n.off_order = t, order
                    kept[z] = []
                pedal[z] = b >= 64
            elif kind == 0xB0 and a == 74 and c in cfg.filters and c not in zone:
                self.cc74[c].append((t, order, b))
                st['cc74'] += 1
            elif kind == 0xB0 and a == 74 and z != c:   # an MPE member's slide
                self.zacts[z].append((t, order, 'slide', c, b))
                st['mpe_slides'] += 1
            elif kind == 0xB0 and (cfg.rpn or c in zone) and (
                    a in (98, 99, 100, 101) or a in (6, 38) and (rpn[c] == (0, 0) or zone and rpn[c] == (0, 6))):
                if a in (100, 101):
                    sel = rpn[c] or (127, 127)
                    rpn[c] = (b, sel[1]) if a == 101 else (sel[0], b)
                    st['rpn_selects'] += 1
                    self.ctl_cc(c, t, order, a, b)       # it deselects --ctl's NRPN
                elif a in (98, 99):                      # an NRPN: data entry goes to it, and is ignored
                    rpn[c] = None
                    if not self.ctl_cc(c, t, order, a, b):
                        self.ccs[c][a] += 1
                elif rpn[c] == (0, 6):                   # --mpe: an MPE configuration message, ignored
                    st['mcm'] += 1
                else:                                    # data entry to RPN 0: CC 6 semitones, CC 38 cents
                    bend[c] = (b, 0) if a == 6 else (bend[c][0], min(b, 99))
                    if z != c:                           # a member's: every member's range, so CC 38 on any
                        for m in zone:                   # member adds its cents to the zone's semitones
                            if zone[m] == z and m != z:
                                bend[m] = bend[c]
                    r = bend[c][0] + Fraction(bend[c][1], 100)
                    if c in zone:
                        self.zacts[z].append((t, order, 'range', c, r))
                    else:
                        self.ranges[c].append((t, order, r))
                    st['rpn_ranges'] += 1
            elif kind == 0xB0:
                if not self.ctl_cc(c, t, order, a, b):
                    self.ccs[c][a] += 1
            elif kind == 0xC0:
                st['programs'] += 1
                if cfg.programs is not None and patch[c] is not None and c in cfg.maps and c not in zone:
                    if c == PERCUSSION:
                        st['programs_percussion'] += 1
                    elif a + 1 in cfg.programs:
                        patch[c] = (c, a + 1)
                        st['programs_applied'] += 1
                    else:
                        patch[c] = (c, None)
                        st['programs_unlisted'] += 1
            elif kind == 0xE0:
                if c in zone:
                    self.zacts[z].append((t, order, 'bend', c, a | (b << 7)))
                else:
                    self.bends[c].append((t, order, a | (b << 7)))
                st['bends'] += 1
            elif kind == 0xA0 and ('polyat',) in cfg.ctls.get(c, ()):
                self.ctl_msg(c, t, order, ('polyat',), q7(b), a, 'poly pressure %d on key %d' % (b, a))
            elif kind == 0xA0:
                st['aftertouch'] += 1
            elif z != c:                                 # an MPE member's channel pressure
                self.zacts[z].append((t, order, 'press', c, a))
                st['mpe_pressure'] += 1
            elif ('pressure',) in cfg.ctls.get(c, ()):
                self.ctl_msg(c, t, order, ('pressure',), q7(a), None, 'pressure %d' % a)
            else:
                st['pressure'] += 1
        for c in range(16):
            st = self.st[c]
            for n in [n for q in held[c].values() for n in q] + kept[c]:
                if n.off is not None:                    # a choke ended it
                    continue
                n.off, n.off_order = self.end_ns, LAST
                if n.patch is not None:
                    st['unreleased'] += 1
            for n in self.notes[c]:
                if n.patch is None:
                    st['skipped'] += 1
                    continue
                st['notes'] += 1
                if n.off - n.on < MIN_NOTE_NS:
                    n.off = n.on + MIN_NOTE_NS
                    st['short'] += 1
            if cfg.mapped(c):
                self.groove(c)
                self.expand(c)
                self.arp_conditions(c)

    def choke(self, c, key, t, order, held, kept):
        """--choke: a note-on of key ends the sounding notes of its groups' other keys at t."""
        others = self.cfg.chokes[c].get(key, ())
        for n in [n for k in others for n in held.get(k, ())] + [n for n in kept if n.key in others]:
            if n.off is None:
                n.off, n.off_order, n.up = t, order, None    # a choke writes no relvel
                self.st[c]['choked'] += 1
        kept[:] = [n for n in kept if n.key not in others]

    def ctl_cc(self, c, t, order, a, b):
        """--ctl: take CC a at value b when channel c's map names it (the docstring's Controllers);
        False leaves it to be counted. CC 98 to 101 select and deselect the NRPN, and are taken on a
        channel that maps one."""
        m = self.cfg.ctls.get(c)
        if not m:
            return False
        if ('cc', a) in m:
            self.ctl_msg(c, t, order, ('cc', a), q7(b), None, 'CC %d at %d' % (a, b))
            return True
        n = a if ('cc+', a) in m else a - 32 if ('cc+', a - 32) in m else None
        if n is not None:
            v = self.wide[c].setdefault(n, [0, 0])
            v[:] = [b, 0] if n == a else [v[0], b]       # an MSB clears the LSB
            x = v[0] << 7 | v[1]
            self.ctl_msg(c, t, order, ('cc+', n), q14(x), None, 'CC %d+ at %d' % (n, x))
            return True
        if a in (98, 99, 100, 101):
            sel = self.nsel[c] or (127, 127)
            self.nsel[c] = None if a in (100, 101) else (b, sel[1]) if a == 99 else (sel[0], b)
            return any(s[0] == 'nrpn' for s in m)
        key = ('nrpn',) + self.nsel[c] if self.nsel[c] else None
        if a in (6, 38) and key in m:
            v = self.ndata[c].setdefault(self.nsel[c], [0, 0])
            v[:] = [b, 0] if a == 6 else [v[0], b]       # data entry: CC 6 the MSB, which clears the LSB
            x = v[0] << 7 | v[1]
            self.ctl_msg(c, t, order, key, q14(x), None, 'NRPN %d.%d at %d' % (key[1], key[2], x))
            return True
        return False

    def ctl_msg(self, c, t, order, source, value, key, label):
        """Keep one message --ctl takes; key is the note's key of poly aftertouch, else None."""
        self.ctlmsgs[c].append((t, order, source, value, key, label))
        self.st[c]['ctl_messages'] += 1

    def groove(self, c):
        """Replace paired notes with arp/chord strikes, before the existing expansion."""
        cfg, source, out, st = self.cfg, self.notes[c], [], self.st[c]
        if c in cfg.arps:
            mode, rate, octaves, gate, _ = cfg.arps[c]
            step = int(4 * self.info['tpqn'] * rate)
            presses = [(n.on, n.off, n.key, n.vel, k) for k, n in enumerate(source)]
            for n in source:
                if n.key + 12 * (octaves - 1) > 127:
                    raise ValueError('--arp channel %d: tick %s key %d plus OCT is outside MIDI 0..127'
                                     % (c + 1, n.tick, n.key))
            def steps():
                for tick in range(0, self.info['end_tick'], step):
                    on, off = self.tm.ns(tick), self.tm.ns(tick + step * gate)
                    if off <= on:
                        raise ValueError('--arp channel %d: gate at tick %d collapses after ns rounding'
                                         % (c + 1, tick))
                    yield on, off
            selected = compose._arp_key_steps(presses, steps(), mode=mode, octaves=octaves,
                                               latch=c in cfg.arp_latches,
                                               seed=compose.mix32(cfg.arp_seeds.get(c, 0)))
            for g, (on, off, chosen) in enumerate(selected):
                for key, vel, identity in chosen:
                    end = off
                    if end - on < MIN_NOTE_NS:
                        end = on + MIN_NOTE_NS
                        st['short'] += 1
                    n = source[identity]
                    out.append(n.copy(n.patch, key=key, vel=vel, on=on, off=end,
                                      order=LAST + len(out), off_order=LAST, up=None, _arp_step=g))
        elif c in cfg.chords:
            shape, spread = cfg.chords[c]
            size = len(shape)
            for n in source:
                if n.key + shape[-1] > 127:
                    raise ValueError('--chord channel %d: tick %s key %d plus SHAPE is outside MIDI 0..127'
                                     % (c + 1, n.tick, n.key))
                if size > 1 and spread >= n.off - n.on:
                    raise ValueError('--chord channel %d: tick %s key %d strum reaches its release'
                                     % (c + 1, n.tick, n.key))
                for k, offset in enumerate(shape):
                    delta = (2 * k * spread + size - 1) // (2 * (size - 1)) if size > 1 else 0
                    out.append(n.copy(n.patch, key=n.key + offset, on=n.on + delta))
        else:
            return
        st['groove_source'], st['groove_generated'] = len(source), len(out)
        st['notes'] += len(out) - len(source)
        self.notes[c] = out

    def expand(self, c):
        """The notes channel c plays, by the docstring's Kits, Layers and splits, Scala, Random and
        Unison: each note made one note a patch, given --random's offsets, and copied onto
        --unison's voices."""
        cfg, st = self.cfg, self.st[c]
        kit, zones, out = cfg.kits.get(c), cfg.zones.get(c), []
        for n in self.notes[c]:
            if kit is not None:
                e = kit.get(n.key)
                if e is None:
                    st['kit_skipped'] += 1
                else:
                    out.append(n.copy((c, ('k', n.key)), fixed=compose.mhz_m(n.key) if e[2] is None else e[2]))
                continue
            if c in cfg.scales:
                try:
                    n.locked_key = compose.scale_lock(n.key, *cfg.scales[c])
                except ValueError as e:
                    raise ValueError('--scale channel %d: %s' % (c + 1, e)) from None
            if cfg.tuning is not None and cfg.tuning[n.pitch_key()] is None:
                st['unmapped_keys'] += 1
                continue
            if zones is None:
                out.append(n)
                continue
            mine = ([n] if c in cfg.maps else []) + [n.copy((c, ('z', i))) for i, z in enumerate(zones)
                                                     if z[0] <= n.key <= z[1]]
            if not mine:
                st['outside'] += 1
            st['layered'] += max(len(mine) - 1, 0)
            out += mine
        st['notes'] -= st['kit_skipped'] + st['unmapped_keys'] + st['outside']
        if c in cfg.randoms:
            seed, cents, vel, _ = cfg.randoms[c]
            x, c100, draws = seed, int(cents * 100), {}
            for strike in sorted({(n.on, n.order) for n in self.notes[c]}):  # each onset/source strike
                x = xorshift32(x)
                dc = x % (2 * c100 + 1) - c100
                x = xorshift32(x)
                draws[strike] = (Fraction(dc, 100), x % (2 * vel + 1) - vel)
            for n in out:
                dc, dv = draws[(n.on, n.order)]
                n.cents += dc
                n.vel = min(max(n.vel + dv, 1), 127)
        if c in cfg.velocity_zones:
            before = Counter((n.on, n.order) for n in out)
            kept = []
            for n in out:
                sub = n.patch[1]
                if not isinstance(sub, tuple) or sub[0] != 'z':
                    kept.append(n)
                    continue
                i = sub[1]
                lo, hi, marked = cfg.velocity_zones[c][i]
                if not lo <= n.vel <= hi:
                    continue
                if marked:
                    n._xfade_a = 0
                    for j in cfg.fade_pairs.get((c, i), ()):
                        z = zones[j]
                        vlo, vhi, _ = cfg.velocity_zones[c][j]
                        if z[0] <= n.key <= z[1] and vlo <= n.vel <= vhi:
                            n._xfade_a = crossfade_attenuation(n.vel, max(lo, vlo), min(hi, vhi), lo > vlo)
                            break
                kept.append(n)
            after = Counter((n.on, n.order) for n in kept)
            skipped = len(before.keys() - after.keys())
            st['velocity_skipped'] += skipped
            st['notes'] -= skipped
            st['layered'] += sum(max(n - 1, 0) for n in after.values()) - sum(max(n - 1, 0) for n in before.values())
            out = kept
        if c in cfg.unisons:
            nv, cents, _ = cfg.unisons[c]
            out = [n.copy(n.patch, cents=n.cents - cents / 2 + i * cents / (nv - 1)) for n in out for i in range(nv)]
        self.notes[c] = out

    def arp_conditions(self, c):
        """Filter whole logical steps after expansion; all random draws already ran."""
        if c not in self.cfg.arps:
            return
        source = self.cfg.arp_neighbors.get(c)
        neighbors = self.arp_played[source] if source is not None else set()
        conditions = self.cfg.arp_conditions.get(c, {})
        groups = {}
        for note in self.notes[c]:
            groups.setdefault(note._arp_step, []).append(note)
        step = int(4 * self.info['tpqn'] * self.cfg.arps[c][1])
        count = (self.info['end_tick'] + step - 1) // step
        pre, out = False, []
        for g in range(count):
            notes = groups.get(g, ())
            condition = conditions.get(g)
            played = bool(notes) and compose.cond_holds(condition, 0, (), pre=pre, nei=g in neighbors)
            if condition is not None:
                pre = played                                  # empty/filtered steps record false
            if played:
                out.extend(notes)
                self.arp_played[c].add(g)
        # Existing notes counts generated strikes, not expanded copies.
        skipped = len({(n.on, n.order) for n in self.notes[c]} - {(n.on, n.order) for n in out})
        self.st[c]['condition_skipped'] += skipped
        self.st[c]['notes'] -= skipped
        self.notes[c] = out

    def pool(self, c, key):
        """The Pool of patch key on channel c, made at its first note: key[1] is None (the --map
        entry), a program, ('z', i) (the channel's key range i) or ('k', KEY) (a --kit entry)."""
        if key not in self.pools[c]:
            pool = self.pool_templates.pop(key, None) or configured_pool(self.cfg, key)
            if key in self.pool_caps:
                pool.max = self.pool_caps[key]
            if pool.max <= 0:
                raise ValueError('pool patch %s has zero capacity' % pool.name)
            self.pools[c][key] = pool
        return self.pools[c][key]

    def split_pool(self):
        """ static release-inclusive demand, then one bounded round-robin split."""
        cfg, demands, caps, limits = self.cfg, {}, {}, {}
        by_patch = {}
        for notes in self.notes:
            for n in notes:
                if n.patch is not None:
                    by_patch.setdefault(n.patch, []).append(n)
        for key in cfg.patch_order:
            c = key[0]
            name = patch_spec(cfg, key)[0]
            limit = cfg.limits.get(name, 1 if c in cfg.monos else cfg.voices)
            reserve = cfg.reserves.get(name, 0)
            if reserve > limit:
                raise ValueError('--reserve %s=%d exceeds limit%d' % (name, reserve, limit))
            demand = 0
            if key in by_patch:
                pool = configured_pool(cfg, key)
                self.pool_templates[key] = pool
                endpoints = [(n.on, 1) for n in by_patch[key]]
                endpoints += [(n.off + pool.rel, -1) for n in by_patch[key]]
                active = 0
                for _, delta in sorted(endpoints):
                    active += delta
                    demand = max(demand, active)
            limits[key], demands[key], caps[key] = limit, min(demand, limit), reserve
        remaining = cfg.pool_total - sum(caps.values())
        if remaining < 0:
            raise ValueError('--reserve total%d exceeds --pool%d' % (sum(caps.values()), cfg.pool_total))
        while remaining:
            eligible = [key for key in cfg.patch_order if caps[key] < demands[key]]
            if not eligible:
                break
            for key in eligible:
                if not remaining:
                    break
                caps[key] += 1
                remaining -= 1
        self.pool_caps = caps
        for c, notes in enumerate(self.notes):
            before = {(n.on, n.order) for n in notes}
            kept = [n for n in notes if n.patch is None or caps[n.patch] > 0]
            self.st[c]['pool_skipped'] += len(notes) - len(kept)
            self.st[c]['notes'] -= len(before - {(n.on, n.order) for n in kept})
            self.notes[c] = kept

    def freq(self, key, semis):
        f = note_mhz(key, semis, self.cfg.tuning)
        if not 0 <= f <= 0xFFFFFFFF:
            self.clamped += 1
            f = min(max(f, 0), 0xFFFFFFFF)
        return f

    def freq_of(self, n, semis):
        """Note n's millihertz bent by semis semitones and moved by its cents (Unison, Random); a
        kit entry's stays at its own pitch, moved only by its cents."""
        if n.fixed is not None:
            f = n.fixed if not n.cents else pitch_mhz(n.fixed, n.cents, 1200)
            return min(max(f, 0), 0xFFFFFFFF)
        return self.freq(n.pitch_key(), semis + n.cents / 100 if n.cents else semis)

    def label_of(self, n, semis):
        if n.fixed is not None:
            return 'key %d' % n.key
        return compose.label(n.pitch_key() + float(semis + n.cents / 100 if n.cents else semis))

    def sounding(self, c, t):
        """(pool, voice, slot) of each voice of channel c still sounding at t, held or released."""
        return [(pool, k, slot) for pool in self.pools[c].values() for k, slot in enumerate(pool.slots)
                if slot[0].off + pool.rel > t]

    def move_filter(self, c, t, pool, k, slot, cc):
        """A filter event when CC 74 at cc puts this voice's cutoff elsewhere than it is."""
        mode, base, qm = pool.filter
        cm = min(max(pitch_mhz(base, cc - 64, 16), 1), 0xFFFFFFFF)
        if cm != slot[2]:
            slot[2] = cm
            self.move(c, (t, 3, pool, k, (cm, qm), 'CC 74 at %d' % cc))
            self.st[c]['filter_events'] += 1

    def move(self, c, event):
        """Append a pitch or filter event, remembered until a note-on at its ns takes its voice."""
        t, _, pool, k = event[:4]
        key, out = (c, pool.name, k), self.out[c]
        last = self.moved.get(key)
        if last and last[0] == t:
            last[1].append(len(out))
        else:
            self.moved[key] = (t, [len(out)])
        out.append(event)

    def take(self, c, t, pool, k):
        """Drop the pitch and filter events written at t for voice k of pool, which a note-on at t
        takes: they would sort after that note-on and move the new note."""
        last = self.moved.pop((c, pool.name, k), None)
        if last and last[0] == t:
            for i in last[1]:
                e = self.out[c][i]
                self.st[c]['filter_events' if e[1] != 2 else 'glide_events' if e[5].startswith('glide ')
                           else 'pitch_events'] -= 1
                self.st[c]['moves_dropped'] += 1
                self.out[c][i] = None

    def write_ctl(self, c, t, pool, k, n, value, label):
        """--ctl: a ctl event when input n, which voice k's patch reads, changes on that voice."""
        latch = pool.latch.setdefault(k, [0] * 8)        # the engine zeroes a bank only at voice init
        if n in pool.reads and latch[n] != value:
            latch[n] = value
            self.out[c].append((t, 4, pool, k, (n, value), label))
            self.st[c]['ctl_events'] += 1

    def ctl_acts(self, c):
        """--ctl's acts for allocate: the offs that may write relvel, put first so that at one (ns,
        file order) they come before the re-strike's or choke's note-on that ends their note, and
        the messages, each with whether a merge keeps it (a run of one source, of one key for
        polyat, merges as bends do)."""
        m = self.cfg.ctls.get(c)
        if not m:
            return [], []
        offs = [(n.off, n.off_order, 5, n) for n in self.notes[c]
                if ('relvel',) in m and n.patch is not None and n.up is not None and n.off_order != LAST]
        msgs, runs, keep = self.ctlmsgs[c], {}, set()
        for i, e in enumerate(msgs):
            runs.setdefault((e[2], e[4]), []).append(i)
        for run in runs.values():
            keep.update(run[j] for j in merged([msgs[i][0] for i in run]))
        return offs, [(e[0], e[1], 4, (e, i in keep)) for i, e in enumerate(msgs)]

    def release_ctl(self, c, pool, k, n, done):
        """--ctl: relvel for note n, which ends on voice k at n.off, written just before its off."""
        m = self.cfg.ctls[c]
        if ('relvel',) in m and n.up is not None and n.off_order != LAST and id(n) not in done:
            done.add(id(n))
            self.write_ctl(c, n.off, pool, k, m[('relvel',)], q7(n.up), 'release velocity %d' % n.up)

    def allocate(self, c):
        """Channel c's notes onto voices, and its kept bends and CC 74 moves onto the voices
        sounding then, in time order (file order at one ns)."""
        st, out = self.st[c], self.out[c]
        bends, moves = self.bends[c], self.cc74[c]
        kb, km = set(merged([e[0] for e in bends])), set(merged([e[0] for e in moves]))
        acts = [(n.on, n.order, 0, n) for n in self.notes[c] if n.patch is not None]
        acts += [(t, order, 1, (v, k in kb)) for k, (t, order, v) in enumerate(bends)]
        acts += [(t, order, 2, (v, k in km)) for k, (t, order, v) in enumerate(moves)]
        acts += [(t, order, 3, r) for t, order, r in self.ranges[c]]
        offs, msgs = self.ctl_acts(c)
        acts = offs + acts + msgs + self.zone_acts(c)
        acts.sort(key=lambda x: x[:2])
        semis, cc, rng = Fraction(0), 64, self.cfg.bend
        m, cur, done = self.cfg.ctls.get(c), [0] * 8, set()      # --ctl: the map, the channel's values
        rand = next(((s[1], n) for s, n in m.items() if s[0] == 'rand'), None) if m else None
        rx = rand[0] if rand else 0                               # rand's own xorshift32 stream
        zone = c in self.cfg.mpe                                  # --mpe: c is a zone's master
        mrng, mb, latest, mcur = Fraction(48), {}, {}, {}         # the member range; by member: bend, note, ctls
        for t, _, kind, x in acts:
            if kind == 0:
                pool = self.pool(c, x.patch)
                k = pool.pick(t, st)
                slot = pool.slots[k]
                if slot[0] is not None:
                    if m and slot[0].off <= t:
                        self.release_ctl(c, pool, k, slot[0], done)
                    slot[0].off = min(slot[0].off, t)    # a steal ends the note it takes now
                    out.append((slot[0].off, 0, pool, k, None, ''))
                    self.take(c, t, pool, k)
                if zone and x.mch != c:                  # a member's note: its bend, and the member's latest
                    x.mb = mb.get(x.mch, Fraction(0))
                    latest[x.mch] = x
                s = semis + x.mb if zone else semis
                slot[:] = [x, self.freq_of(x, s), None]
                if rand:
                    rx = xorshift32(rx)                  # one draw a voice note-on, read or not
                for n in pool.reads if m or zone else ():
                    if n == pool.crossfade_input:
                        continue                         # its explicit value follows the old inputs
                    if rand and n == rand[1]:
                        self.write_ctl(c, t, pool, k, n, (rx >> 16) - 32768, 'random')
                    elif zone and n in (1, 4):
                        self.write_ctl(c, t, pool, k, n, mcur.get(x.mch, [0] * 8)[n], 'at the note-on')
                    else:
                        self.write_ctl(c, t, pool, k, n, cur[n], 'at the note-on')
                if pool.crossfade_input is not None:
                    out.append((t, 4, pool, k, (pool.crossfade_input, crossfade_ctl(x._xfade_a)),
                                'velocity crossfade %d dB10' % x._xfade_a))
                    st['ctl_events'] += 1
                out.append((t, 1, pool, k, (slot[1], x.vel * 32767 // 127), self.label_of(x, s)))
                if pool.filter:
                    slot[2] = pool.filter[1]             # a note-on resets the voice to its patch's cutoff
                    self.move_filter(c, t, pool, k, slot, cc)
            elif kind == 1:
                v, keep = x
                semis = Fraction(v - 8192) * rng / 8192
                if keep:
                    st['bends_kept'] += 1
                    self.bend_to(c, t, semis)
            elif kind == 2:
                cc, keep = x
                if keep:
                    st['cc74_kept'] += 1
                    for pool, k, slot in self.sounding(c, t):
                        self.move_filter(c, t, pool, k, slot, cc)
            elif kind == 3:
                rng = x                                  # RPN 0: the bends after it
            elif kind == 6:                              # --mpe: a zone's bend, range, slide or pressure
                what, ch, v, keep = x
                if what == 'range':
                    rng, mrng = (v, mrng) if ch == c else (rng, v)
                elif what == 'bend' and ch == c:
                    semis = Fraction(v - 8192) * rng / 8192
                    if keep:
                        st['bends_kept'] += 1
                        self.bend_zone(c, t, semis, None)
                elif what == 'bend':
                    mb[ch] = Fraction(v - 8192) * mrng / 8192
                    if keep:
                        self.st[ch]['bends_kept'] += 1
                        if ch in latest:
                            latest[ch].mb = mb[ch]
                            self.bend_zone(c, t, semis, latest[ch])
                else:
                    n = 4 if what == 'slide' else 1
                    mcur.setdefault(ch, [0] * 8)[n] = q7(v)
                    if keep:
                        self.st[ch]['mpe_kept'] += 1
                        for pool, k, slot in self.sounding(c, t):
                            if slot[0] is latest.get(ch):
                                self.write_ctl(c, t, pool, k, n, q7(v), '%s %d on channel %d' % (
                                    'slide' if n == 4 else 'pressure', v, ch + 1))
            elif kind == 4:
                e, keep = x
                n = m[e[2]]
                if e[4] is None:
                    cur[n] = e[3]                        # the channel's value, for the next note-ons
                if keep:
                    st['ctl_kept'] += 1
                    for pool, k, slot in self.sounding(c, t):
                        if e[4] is None or slot[0].origin_key == e[4]:
                            self.write_ctl(c, t, pool, k, n, e[3], e[5])
            else:
                pool = self.pools[c][x.patch]            # an off: relvel when the note still has its voice
                k = next((k for k, slot in enumerate(pool.slots) if slot[0] is x), None)
                if k is not None:
                    self.release_ctl(c, pool, k, x, done)
        for pool in self.pools[c].values():
            for k, slot in enumerate(pool.slots):
                out.append((slot[0].off, 0, pool, k, None, ''))
        self.out[c] = [e for e in out if e is not None]

    def zone_acts(self, c):
        """--mpe's acts for allocate on master c: each (t, order, 6, (what, channel, value, kept)), a
        run of one channel's bends, slides or pressure merging as bends do; a range is always kept."""
        za, runs, keep = self.zacts[c], {}, set()
        for i, e in enumerate(za):
            if e[2] != 'range':
                runs.setdefault(e[2:4], []).append(i)
        for run in runs.values():
            keep.update(run[j] for j in merged([za[i][0] for i in run]))
        return [(e[0], e[1], 6, (e[2], e[3], e[4], e[2] == 'range' or i in keep)) for i, e in enumerate(za)]

    def bend_zone(self, c, t, semis, only):
        """--mpe: a pitch event for each voice of zone c sounding at t (only note only's when given)
        whose pitch, its key plus the master's semis plus its note's member bend, changes."""
        for pool, k, slot in self.sounding(c, t):
            if only is not None and slot[0] is not only:
                continue
            s = semis + slot[0].mb
            f = self.freq_of(slot[0], s)
            if f != slot[1]:
                slot[1] = f
                self.move(c, (t, 2, pool, k, f, self.label_of(slot[0], s)))
                self.st[c]['pitch_events'] += 1

    def bend_to(self, c, t, semis, skip=None):
        """A pitch event for each voice of channel c sounding at t whose frequency semis changes."""
        for pool, k, slot in self.sounding(c, t):
            if pool is skip:
                continue
            f = self.freq_of(slot[0], semis)
            if f != slot[1]:
                slot[1] = f
                self.move(c, (t, 2, pool, k, f, self.label_of(slot[0], semis)))
                self.st[c]['pitch_events'] += 1

    def mono_plan(self, c):
        """Channel c's notes under --mono (the docstring's Mono): [(t, order, what, note)], what 0
        for an off, 1 for a note-on of note, 2 for a legato move to note."""
        mode, prio = self.cfg.monos[c]
        notes = [n for n in self.notes[c] if n.patch is not None]
        keys = sorted([(n.on, n.order, 1, i) for i, n in enumerate(notes)] +
                      [(n.off, n.off_order, 0, i) for i, n in enumerate(notes)])
        plan, held, cur, j = [], [], None, 0
        while j < len(keys):
            t, gap = keys[j][0], False
            while j < len(keys) and keys[j][0] == t:     # the key events at one ns, in file order
                _, order, on, i = keys[j]
                j += 1
                held.append(notes[i]) if on else held.remove(notes[i])
                gap = gap or (cur is not None and not held)
            if not held:
                new = None
            elif prio == 'last':
                new = held[-1]
            else:                                         # of equal keys, the one struck last
                new = (min if prio == 'low' else max)(reversed(held), key=lambda n: n.key)
            if new is cur and not gap:
                continue
            if new is None:
                plan.append((t, order, 0, cur))
            elif cur is None or gap or mode == 'retrigger' or new.patch != cur.patch:
                plan.append((t, order, 1, new))
            else:
                plan.append((t, order, 2, new))
            cur = new
        return plan

    def allocate_mono(self, c):
        """Channel c under --mono: one voice a patch, played by mono_plan, with its bends, CC 74
        moves and RPN 0 ranges in time order (file order at one ns) and --glide's pitch events."""
        cfg, st, out = self.cfg, self.st[c], self.out[c]
        mode, glide = cfg.monos[c][0], cfg.glides.get(c)
        plan = self.mono_plan(c)
        segs, last = [], None                             # the note each note-on starts, ended by the next off
        for t, order, what, n in plan:
            if what == 1:
                seg = n.copy(n.patch, on=t)
                seg.off = None
                if last is not None and last.off is None:
                    last.off = t                          # a retrigger, or a note-on on another patch
                segs.append(seg)
                last = seg
            elif what == 0:
                last.off = t
        bends, moves = self.bends[c], self.cc74[c]
        kb, km = set(merged([e[0] for e in bends])), set(merged([e[0] for e in moves]))
        heap = [(t, order, 0, k, (what, n)) for k, (t, order, what, n) in enumerate(plan)]
        heap += [(t, order, 1, 0, (v, k in kb)) for k, (t, order, v) in enumerate(bends)]
        heap += [(t, order, 2, 0, (v, k in km)) for k, (t, order, v) in enumerate(moves)]
        heap += [(t, order, 3, 0, r) for t, order, r in self.ranges[c]]
        heapq.heapify(heap)
        semis, cc, rng, held, gen, seq, ons = Fraction(0), 64, cfg.bend, None, 0, 0, iter(segs)
        gliding = None                                    # generation, pool, scheduled target mHz
        while heap:
            t, order, kind, _, x = heapq.heappop(heap)
            if kind == 0:
                what, n = x
                if what == 0:
                    out.append((t, 0, held, 0, None, ''))
                    held = None
                    continue
                if what == 2:                             # a legato move: a pitch event, no note-on
                    slot = held.slots[0]
                    seg = slot[0]
                    seg.key, seg.cents, seg.fixed = n.key, n.cents, n.fixed
                    f1 = self.freq_of(seg, semis)
                    gen += 1
                    st['legato_moves'] += 1
                    if glide and f1 != slot[1]:
                        seq = self.glide(heap, t, order, seq, gen, held, slot[1], f1, glide, self.label_of(seg, semis))
                        gliding = (gen, held, f1)
                    elif f1 != slot[1]:
                        slot[1] = f1
                        self.move(c, (t, 2, held, 0, f1, self.label_of(seg, semis)))
                        st['pitch_events'] += 1
                    continue
                seg = next(ons)
                pool = self.pool(c, seg.patch)
                if not pool.slots:
                    pool.slots.append([None, 0, None])
                slot = pool.slots[0]
                if held is not None and held is not pool:
                    out.append((t, 0, held, 0, None, ''))   # a note-on on another patch ends this one
                sounding = slot[0] is not None and slot[0].off + pool.rel > t
                st['mono_ons'] += 1
                st['retriggers'] += sounding              # a note-on to a sounding voice
                if slot[0] is not None:
                    self.take(c, t, pool, 0)
                f1, gen, held = self.freq_of(seg, semis), gen + 1, pool
                f0 = slot[1] if glide and mode == 'retrigger' and sounding else f1
                slot[:] = [seg, f0, None]
                out.append((t, 1, pool, 0, (f0, seg.vel * 32767 // 127),
                            self.label_of(seg, semis) + (', glides in' if f0 != f1 else '')))
                if f0 != f1:
                    seq = self.glide(heap, t, order, seq, gen, pool, f0, f1, glide, self.label_of(seg, semis))
                    gliding = (gen, pool, f1)
                if pool.filter:
                    slot[2] = pool.filter[1]
                    self.move_filter(c, t, pool, 0, slot, cc)
            elif kind == 1:
                v, keep = x
                semis = Fraction(v - 8192) * rng / 8192
                if keep:
                    st['bends_kept'] += 1
                    unchanged = (gliding is not None and gliding[0] == gen
                                 and self.freq_of(gliding[1].slots[0][0], semis) == gliding[2])
                    if not unchanged:
                        gen += 1                          # a changed bend ends the current glide
                    self.bend_to(c, t, semis, gliding[1] if unchanged else None)
            elif kind == 2:
                cc, keep = x
                if keep:
                    st['cc74_kept'] += 1
                    for pool, k, slot in self.sounding(c, t):
                        self.move_filter(c, t, pool, k, slot, cc)
            elif kind == 3:
                rng = x
            else:                                         # a glide's step
                g, pool, f, text = x
                slot = pool.slots[0]
                if g == gen and slot[0].off + pool.rel > t and f != slot[1]:
                    slot[1] = f
                    self.move(c, (t, 2, pool, 0, f, text))
                    st['glide_events'] += 1
        self.out[c] = [e for e in out if e is not None]

    def glide(self, heap, t, order, seq, gen, pool, f0, f1, ms, text):
        """--glide's steps from f0 to f1 mHz after t: n = ceil(ms / GLIDE_STEP_MS) pitch events
        equal in cents, the i-th at t + i * ms / n (rounded half up to the ns), the last on f1."""
        n = -(-ms // GLIDE_STEP_MS)
        for i in range(1, n + 1):
            ti = t + (2 * i * ms * 1000000 + n) // (2 * n)
            f = glide_mhz(f0, f1, i, n)
            seq += 1
            heapq.heappush(heap, (ti, order, 4, seq, (gen, pool, f, 'glide %d/%d to %s' % (i, n, text))))
        return seq

    # -- what it makes ------------------------------------------------------
    def channels(self):
        """The channels that sound: mapped, with a note."""
        return [c for c in range(16) if self.pools[c]]

    def stream_name(self, c):
        return self.cfg.maps[c][2]

    def streams_of(self, c):
        """The streams of channel c's patches, in the order of the patches' first notes."""
        return list(dict.fromkeys(pool.stream for pool in self.pools[c].values()))

    def voices(self, c):
        return sum(len(pool.slots) for pool in self.pools[c].values())

    def length_ns(self):
        last = max([self.end_ns] + [e[0] for c in range(16) for e in self.out[c]])
        tail = self.cfg.tail_ms
        if tail is None:
            rel = max([pool.rel_ms for c in range(16) for pool in self.pools[c].values()] or [0])
            tail = 1000 + (5 * rel + 3) // 4
        return last + tail * 1000000

    def problems(self):
        """Why the harness would refuse this piece, if it would."""
        out, chans = [], self.channels()
        if not chans:
            out.append('no mapped channel has a note: map one with --map CH=RECIPE')
        patches = sum(len(self.pools[c]) for c in chans)
        voices = sum(self.voices(c) for c in chans)
        if patches > 32:
            out.append('%d patches, and the harness takes 32: list fewer programs' % patches)
        if voices > 64:
            out.append('%d voices, and the harness takes 64: lower --voices' % voices)
        if self.length_ns() * self.cfg.fs // 10 ** 9 > 0xFFFFFFFF:
            out.append('longer than the harness takes')
        return out

    def summary(self, name, src):
        """The counts, as lines to print."""
        i, cfg = self.info, self.cfg
        div = ('%d ticks a quarter note' % i['tpqn'] if 'tpqn' in i
               else 'SMPTE %d frames a second, %d ticks a frame' % i['smpte'])
        out = ['%s: format %d, %s, %s, %s; the file ends at %s s' % (
            os.path.basename(src), i['format'], count(i['tracks'], 'track'), div,
            count(i['tempo_events'], 'tempo event'), secs(self.end_ns))]
        past = []
        if i['header_tracks'] != i['tracks']:
            past.append('a header that says %d tracks' % i['header_tracks'])
        if i['sysex']:
            past.append('%d SysEx' % i['sysex'])
        if i['meta']:
            past.append('meta events %s' % ', '.join('%02X x%d' % kv for kv in sorted(i['meta'].items())))
        for key, before, word, after in (('crossed', 'running status kept across a meta or SysEx event ', 'time', ''),
                                         ('no_end', '', 'track', ' with no end of track'),
                                         ('after_end', '', 'byte', ' after an end of track'),
                                         ('other_chunks', '', 'chunk', ' other than MTrk'),
                                         ('trailing', '', 'byte', ' after the last chunk')):
            if i[key]:
                past.append(before + count(i[key], word) + after)
        if past:
            out.append('  skipped: ' + '; '.join(past))
        for c in range(16):
            st, ccs = self.st[c], self.ccs[c]
            if not st and not ccs and not cfg.mapped(c):
                continue
            said, ignored = [], []
            if cfg.zone_of.get(c, c) != c:
                head = 'ch%d' % (c + 1)
                said.append('MPE member of ch%d: %s, %s (%d kept), %s and %s (%d kept)' % (
                    cfg.zone_of[c] + 1, count(st['mpe_notes'], 'note'), count(st['bends'], 'bend'), st['bends_kept'],
                    count(st['mpe_slides'], 'slide'), count(st['mpe_pressure'], 'pressure message'), st['mpe_kept']))
                for key, word in (('programs', 'program change'), ('rpn_ranges', 'member range')):
                    if st[key]:
                        said.append(count(st[key], word) + (' ignored' if key == 'programs' else ''))
            elif self.pools[c]:
                streams = self.streams_of(c)
                head = 'ch%d -> %s %s' % (c + 1, 'stream' if len(streams) == 1 else 'streams', ', '.join(streams))
                said.append('%s on %s%s' % (count(st['notes'], 'note'), count(self.voices(c), 'voice'), ''.join(
                    ', patch %s (%s)' % (p.name, p.rname) for p in self.pools[c].values())))
                said.append('steals %d, release steals %d' % (st['steals'], st['release_steals']))
                for key, word, after in (('short', 'note', ' lengthened to 1 ms'),
                                         ('unreleased', 'note', ' still on at the end of the file, ended there'),
                                         ('orphan_offs', 'note-off', ' with no note to end'),
                                         ('restruck', 'pedalled note', ' ended by a re-strike'),
                                         ('moves_dropped', 'pitch or filter event',
                                          ' dropped at a note-on that took its voice')):
                    if st[key]:
                        said.append(count(st[key], word) + after)
                if st['pedal_downs'] or st['pedal_held']:
                    said.append('pedal down %s, %s held' % (count(st['pedal_downs'], 'time'),
                                                              count(st['pedal_held'], 'note-off')))
                if st['bends']:
                    said.append('bends %d (%d kept, %s)' % (st['bends'], st['bends_kept'],
                                                            count(st['pitch_events'], 'pitch event')))
                if c in cfg.filters:
                    said.append('CC 74 %d (%d kept, %s)' % (st['cc74'], st['cc74_kept'],
                                                            count(st['filter_events'], 'filter event')))
                if st['programs'] and (cfg.programs is None or c == PERCUSSION):
                    said.append('%s ignored (%s)' % (count(st['programs'], 'program change'),
                                                     'no --program' if cfg.programs is None else 'channel 10'))
                elif st['programs'] and c in cfg.mpe:
                    said.append('%s ignored (an MPE zone plays one patch)' % count(st['programs'], 'program change'))
                elif st['programs'] and c not in cfg.maps:
                    said.append('%s ignored (no plain --map entry)' % count(st['programs'], 'program change'))
                elif st['programs']:
                    said.append('program changes %d (%d to a listed program, %d to another: patch ch%d)' % (
                        st['programs'], st['programs_applied'], st['programs_unlisted'], c + 1))
                said += self.said_modes(c)
            else:
                head = 'ch%d' % (c + 1)
                if cfg.mapped(c):                         # mapped, but with no note: it makes no stream
                    said.append('mapped, no notes')
                    said += self.said_modes(c)
                else:
                    said.append('%s skipped (%s)' % (count(st['skipped'], 'note'),
                                                     'percussion, unmapped' if c == PERCUSSION else 'unmapped'))
                for key, word in (('bends', 'bend'), ('programs', 'program change'), ('pedal_downs', 'pedal down'),
                                  ('cc74', 'CC 74 move')):
                    if st[key]:
                        ignored.append(count(st[key], word))
            for key, word in (('aftertouch', 'aftertouch'), ('pressure', 'channel pressure'),
                              ('mcm', 'MPE configuration message')):
                if st[key]:
                    ignored.append('%s %d' % (word, st[key]))
            ignored += ['CC %d x%d' % kv for kv in sorted(ccs.items())]
            if ignored:
                said.append('ignored: ' + ', '.join(ignored))
            for key, label in (('velocity_skipped', 'velocity-zone strikes skipped'),
                               ('pool_skipped', 'pool voice notes skipped')):
                if st[key]:
                    said.append('%s %d' % (label, st[key]))
            out.append('  %s: %s' % (head, '; '.join(said)))
        chans = self.channels()
        if chans:
            out.append('  piece %s: %s, %s, %s, %s s long%s' % (
                name, count(sum(len(self.streams_of(c)) for c in chans), 'stream'),
                count(sum(len(self.pools[c]) for c in chans), 'patch'),
                count(sum(self.voices(c) for c in chans), 'voice'), secs(self.length_ns()),
                '; %d pitches kept within 0 to 4294967295 mHz' % self.clamped if self.clamped else ''))
        return out

    def said_modes(self, c):
        """The summary's words for the options of Mono, Unison, Kits, Random, Scala and RPN 0."""
        cfg, st, said = self.cfg, self.st[c], []
        if c in cfg.arps:
            mode, _, octaves, gate, text = cfg.arps[c]
            said.append('arp %s %s, octaves %d, gate %s, latch %s, seed %d: %d source notes, %d generated' % (
                mode, text, octaves, gate, 'on' if c in cfg.arp_latches else 'off',
                cfg.arp_seeds.get(c, 0), st['groove_source'], st['groove_generated']))
            if c in cfg.arp_conditions:
                said.append('%d conditional strikes skipped' % st['condition_skipped'])
        if c in cfg.chords:
            shape, spread = cfg.chords[c]
            said.append('chord %s, total spread %d ns: %d source notes, %d generated' % (
                '+'.join(map(str, shape)), spread, st['groove_source'], st['groove_generated']))
        if c in cfg.monos:
            glide = cfg.glides.get(c)
            said.append('mono %s, %s note priority: %s, %s, %s%s' % (
                cfg.monos[c][0], cfg.monos[c][1], count(st['mono_ons'], 'note-on'),
                count(st['retriggers'], 'retrigger'), count(st['legato_moves'], 'legato move'),
                ', glide %d ms (%s)' % (glide, count(st['glide_events'], 'pitch event')) if glide else ''))
        if c in cfg.unisons:
            said.append('unison %d, %s cents wide (%s a note)' % (cfg.unisons[c][0], cfg.unisons[c][2],
                                                               count(cfg.unisons[c][0], 'voice')))
        for key, word, after in (('layered', 'note', ' copied to a second layer'),
                                 ('outside', 'note', ' outside every key range, skipped'),
                                 ('kit_skipped', 'note', ' on a key with no --kit entry, skipped'),
                                 ('choked', 'note', ' ended by a choke'),
                                 ('unmapped_keys', 'note', ' on a key the .kbm leaves unmapped, skipped')):
            if st[key]:
                said.append(count(st[key], word) + after)
        if c in cfg.randoms:
            seed, _, vel, text = cfg.randoms[c]
            said.append('random seed %d, within %s cents, velocity within %d' % (seed, text, vel))
        if st['rpn_selects'] or st['rpn_ranges']:
            said.append('RPN: %s, %s' % (count(st['rpn_selects'], 'select'), count(st['rpn_ranges'], 'bend range')))
        if c in cfg.mpe:
            members = cfg.mpe[c]
            said.append('MPE %s zone, members ch%d to ch%d: %s, %s' % (
                'lower' if c == 0 else 'upper', min(members) + 1, max(members) + 1,
                count(sum(self.st[m]['mpe_notes'] for m in members), 'member note'),
                count(st['ctl_events'], 'ctl event') +
                (' from member pressure and slide' if st['ctl_events'] and c not in cfg.ctls else '')))
        if c in cfg.ctls:
            said.append('--ctl %s: %s (%d kept), %s' % (
                ' '.join('%s:%d' % (ctl_name(s), n) for s, n in cfg.ctls[c].items()),
                count(st['ctl_messages'], 'message'), st['ctl_kept'], count(st['ctl_events'], 'ctl event')))
        return said

    def about(self, name, src):
        """session.tl's header comment: where the piece came from, by which rules and options. The
        options wrap at 96 characters, a longer word broken, so no line passes 1022 bytes."""
        lines = ['%s: converted from %s by %s.' % (
                     name, shown(src), MARK),
                 'The live voice manager belongs to the app; this record is its offline',
                 'form, with %d steals and %d release steals.' % (sum(st['steals'] for st in self.st),
                                                                  sum(st['release_steals'] for st in self.st))]
        return lines + textwrap.wrap('Options: ' + ' '.join(self.cfg.options), 96,
                                     break_long_words=True, break_on_hyphens=False)

    def role(self, c, s):
        """The role in stream s's header line: channel c's patches on s and their recipes, or,
        past ROLE_MAX characters, the patches' names alone (session.tl gives their recipes)."""
        pools = [p for p in self.pools[c].values() if p.stream == s]
        text = 'MIDI channel %d, %s' % (c + 1, ', '.join('patch %s (recipe %s)' % (p.name, p.rname) for p in pools))
        if len(text) > ROLE_MAX:
            text = 'MIDI channel %d, %d patches (session.tl gives their recipes): %s' % (
                c + 1, len(pools), ', '.join(p.name for p in pools))
        return text

    def piece(self, name, out_dir, src):
        """The compose.py Piece of this conversion, its record in out_dir."""
        p = MidiPiece(name, 120, 4, self.about(name, src), fs=self.cfg.fs)
        p.dir = out_dir
        p.crossfades = {}
        for c in self.channels():
            for s in self.streams_of(c):
                p.stream(s, self.role(c, s))
            for pool in self.pools[c].values():
                p.patch(pool.name, pool.car, pool.mod, pool.comment, *pool.extra)
                if pool.crossfade is not None:
                    p.crossfades[pool.name] = pool.crossfade[0]
                if pool.filter:
                    mode, cm, qm = pool.filter
                    p.filter(pool.name, mode, cm / 1000, qm / 1000)
                pool.first = p.add_voices(pool.name, len(pool.slots), 'MIDI channel %d' % (c + 1))[0]
        for c in self.channels():
            for t, kind, pool, k, value, text in self.out[c]:
                s, v = pool.stream, pool.first + k
                if kind == 0:
                    p.off(s, t, v)
                elif kind == 1:
                    p.on_f(s, t, v, value[0], value[1], text)
                elif kind == 2:
                    p.pitch_f(s, t, v, value, text)
                elif kind == 3:
                    p.cutoff(s, t, v, value[0] / 1000, value[1] / 1000, text)
                else:
                    p.ctl(s, t, v, value[0], value[1], text)
        p.length_ns = self.length_ns()
        return p


class MidiPiece(compose.Piece):
    """A Piece whose times come from a tempo map, not a bpm grid, and whose session.tl says it is
    a conversion when it lies outside music."""

    def validate_crossfades(self, levels_db10=None):
        """Check actual writer levels before opening any destination file."""
        for name, maximum in getattr(self, 'crossfades', {}).items():
            compose.carrier_attenuation_levels(
                name, self.patches[name], self.routes.get(name, (None, None)),
                None if levels_db10 is None else levels_db10.get(name), maximum,
                vector=name in self.vectors)

    def write(self, levels_db10=None):
        self.validate_crossfades(levels_db10)
        super().write(levels_db10)
        grid = '# %d bpm: a beat is ' % self.bpm
        for s in self.streams:
            rewrite(os.path.join(self.dir, s + '.tl'), grid, TIMES_NOTE)

    def write_session(self, levels_db10=None, hash_=None):
        self.validate_crossfades(levels_db10)
        super().write_session(levels_db10, hash_)
        rewrite(os.path.join(self.dir, 'session.tl'), '# An unpinned profile render', SESSION_NOTE)
        self._final_levels = None if levels_db10 is None else dict(levels_db10)
        self._final_hash = hash_


def rewrite(path, start, line):
    """Replace each line of path that begins with start by line."""
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.writelines(line if old.startswith(start) else old for old in lines)


class Tee(io.StringIO):
    """Keeps what is printed, and passes it on to echo when echo is a stream."""

    def __init__(self, echo):
        super().__init__()
        self.echo = echo

    def write(self, s):
        if self.echo is not None:
            self.echo.write(s)
        return super().write(s)


def settle_hint(printed):
    """Which way compose.py's leveller failed, read from the last level pass it printed, and what
    to do: it lowers the levels at most 1.5 dB between two of its 8 renders, 10.5 dB in all, and
    raises no level above 0 dB."""
    passes = [line for line in printed.splitlines() if line.startswith('  level pass ')]
    if not passes:
        return ''
    kv = dict(w.split('=', 1) for w in passes[-1].split() if '=' in w)
    if int(kv['clipped']) == 0 and float(kv['peak_dbfs']) < compose.TARGET:
        return ('; the record stays quieter than the %g dBFS target, as no level goes above 0 dB: its'
                ' notes are too soft, or too few sound at once, and this tool has no option to raise them'
                ' yet' % compose.TARGET)
    return ("; the record is too loud for compose.py's leveller, which lowers the levels 10.5 dB at most:"
            " give each mapped channel a quieter carrier, --map 'CH=RECIPE c_level=N' (N dB quieter;"
            ' try 10), and each --program entry the same')


def render(p, renders, echo):
    """compose.py's finish on p, its WAV and merged record in renders: the levels set, the hash
    written, the record rendered and replayed to it. Returns (the PLAY line, the hash)."""
    os.makedirs(renders, exist_ok=True)
    tee, saved = Tee(sys.stdout if echo else None), compose.OUT
    compose.OUT = renders
    try:
        with contextlib.redirect_stdout(tee):
            compose.finish(p)
    except SystemExit as e:                              # compose.py's finish stops with sys.exit
        raise SystemExit('%s%s' % (e, settle_hint(tee.getvalue()) if 'levels did not settle' in str(e) else ''))
    finally:
        compose.OUT = saved
    line = [l for l in tee.getvalue().splitlines() if l.startswith('  record: ')][-1].split(' ', 4)[4]
    return line, dict(w.split('=', 1) for w in line.split()[1:] if '=' in w)['hash']


def render_crossfade(p, echo):
    """Level and replay privately; publish only after all bound checks pass."""
    destination = p.dir
    try:
        with tempfile.TemporaryDirectory(prefix='midi2tl-crossfade-') as tmp:
            p.dir = os.path.join(tmp, 'record')
            result = render(p, os.path.join(tmp, 'renders'), echo)
            levels, hash_ = p._final_levels, p._final_hash
            p.validate_crossfades(levels)
            p.dir = destination
            p.write(levels)
            p.write_session(levels, hash_)
            return result
    finally:
        p.dir = destination


# ---------------------------------------------------------------------------
# Options
class Config:
    """The options, checked: maps {channel: (recipe, deltas, stream)}, programs {program: (recipe,
    deltas)} or None, filters {channel: (mode, cutoff mHz, Q milli)}, voices, bend (semitones, a
    Fraction), tuning (128 mHz) or None, tail_ms or None, and options (as text, for session.tl);
    and the options of Mono to RPN 0: zones {channel: [(lo, hi, recipe, deltas, stream)]}, kits
    {channel: {key: (recipe, deltas, mHz or None)}}, monos {channel: (mode, priority)}, glides
    {channel: ms}, unisons {channel: (n, cents, text)}, chokes {channel: {key: other keys}},
    randoms {channel: (seed, cents, vel, text)}, rpn and ctls {channel: {source: input}}; mpe
    {master: members}, zone_of {zone channel: master}, scales {channel: (mask, root)} and fs;
    arps {channel: (mode, rational rate, octaves, rational gate, rate text)},
    chords {channel: (shape, spread_ns)}, arp_latches and arp_seeds;
    arp_conditions {channel: {global step: condition}} and arp_neighbors {channel: source channel};
    velocity_zones parallel to key zones, crossfades {patch key: maximum dB10},
    fade_pairs, patch_order, optional pool_total and reserves/limits by emitted patch name;
    a --scala table's unmapped keys are None."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def mapped(self, c):
        """Channel c plays: a --map entry, a key range or a --kit names it."""
        return c in self.maps or c in self.zones or c in self.kits


def entries(values):
    """The entries of an option given once or more: split at commas, each into its words."""
    out = []
    for v in values or ():
        for e in v.split(','):
            if not e.split():
                raise ValueError('an empty entry in %r' % v)
            out.append(e.split())
    return out


def recipe_words(rname, words, where):
    """compose.recipe's keyword arguments from the words time=X and any tweak recipe takes:
    c_ARG=DELTA and m_ARG=DELTA, and a routed recipe's o2_ARG, o3_ARG and wF_T ."""
    if rname not in compose.RECIPES:
        raise ValueError('%s: no recipe %s (compose.py --recipes lists them)' % (where, rname))
    kw = {}
    for w in words:
        k, eq, v = w.partition('=')
        if not eq or not compose.TWEAK_VALUE.match(v) or k in kw:
            raise ValueError('%s: %s is not ARG=NUMBER, once' % (where, w))
        kw[k] = float(v) if '.' in v else int(v)
    if kw.get('time', 1) <= 0:
        raise ValueError('%s: time must be above 0' % where)
    try:
        compose.recipe(rname, **kw)
    except TypeError as e:
        raise ValueError('%s: %s' % (where, e)) from None
    return kw


def check_stream(where, stream):
    if not NAME_RE.match(stream):
        raise ValueError('%s: stream %s is not 1 to 31 letters, digits, _ . - (not led by . or -)' % (where, stream))
    if stream.casefold() == 'session':
        raise ValueError("%s: stream %s would be the file %s.tl, which is session.tl%s" % (
            where, stream, stream, '' if stream == 'session' else
            " on a case-insensitive file system (macOS's default); stream names are compared ignoring case"))


def parse_maps(values, *, detailed=False):
    """--map's entries: (maps, zones), the plain entries and the key ranges (Layers and splits)."""
    maps, zones, velocity, order = {}, {}, {}, []
    for words in entries(values):
        z = ZONE_RE.match(words[0])
        m = MAP_RE.match(z.group(1) if z else words[0])
        if not m:
            raise ValueError('--map %s: not CH=RECIPE[:STREAM]%s' % (words[0], '@LO-HI' if z else ''))
        ch, rname = int(m.group(1)), m.group(2)
        if z:
            lo, hi = int(z.group(2)), int(z.group(3))
            if not 1 <= ch <= 16 or not lo <= hi <= 127:
                raise ValueError('--map %s: not CH=RECIPE[:STREAM]@LO-HI with CH 1 to 16 and keys'
                                 ' 0 <= LO <= HI <= 127' % words[0])
            if z.group(6) and not z.group(4):
                raise ValueError('--map %s: ~ requires explicit /VLO-VHI' % words[0])
            if m.group(3):
                check_stream('--map ' + words[0], m.group(3))
            zones.setdefault(ch - 1, []).append(
                (lo, hi, rname, recipe_words(rname, words[1:], '--map ' + words[0]), m.group(3)))
            order.append((ch - 1, ('z', len(zones[ch - 1]) - 1)))
            vlo, vhi = int(z.group(4) or 1), int(z.group(5) or 127)
            if not 1 <= vlo <= vhi <= 127:
                raise ValueError('--map %s: velocity range must satisfy1 <= VLO <= VHI <=127' % words[0])
            if z.group(4) or z.group(6):
                velocity.setdefault(ch - 1, {})[len(zones[ch - 1]) - 1] = (vlo, vhi, bool(z.group(6)))
            continue
        stream = m.group(3) or 'ch%d' % ch
        if not 1 <= ch <= 16 or ch - 1 in maps:
            raise ValueError('--map %s: channel %d is not 1 to 16, or is mapped twice' % (words[0], ch))
        check_stream('--map ' + words[0], stream)
        maps[ch - 1] = (rname, recipe_words(rname, words[1:], '--map ' + words[0]), stream)
        order.append((ch - 1, None))
    for c, zs in zones.items():                        # a range's stream: its own, the entry's, or chCH
        zones[c] = [z[:4] + (z[4] or (maps[c][2] if c in maps else 'ch%d' % (c + 1)),) for z in zs]
    velocity = {c: [given.get(i, (1, 127, False)) for i in range(len(zones[c]))]
                for c, given in velocity.items()}
    return (maps, zones, velocity, order) if detailed else (maps, zones)


def fade_overlaps(zones, velocity):
    """Validate every key/velocity intersection, without choosing an ambiguous direction."""
    pairs, maxima = {}, {}
    for c, bounds in velocity.items():
        zs = zones[c]
        if any(marked for _, _, marked in bounds):
            for key in range(128):
                for v in range(1, 128):
                    overlap = [i for i, z in enumerate(zs)
                               if z[0] <= key <= z[1] and bounds[i][0] <= v <= bounds[i][1]]
                    if len(overlap) > 2 and any(bounds[i][2] for i in overlap):
                        raise ValueError('crossfade channel%d: three or more zones overlap key%d velocity%d' %
                                         (c + 1, key, v))
        for i, (lo, hi, marked) in enumerate(bounds):
            if marked:
                maxima[(c, ('z', i))] = 0
            for j in range(i):
                vlo, vhi, other = bounds[j]
                if max(zs[i][0], zs[j][0]) > min(zs[i][1], zs[j][1]) or max(lo, vlo) > min(hi, vhi):
                    continue
                if not (marked or other):
                    continue
                names = 'ch%dz%d/ch%dz%d' % (c + 1, j + 1, c + 1, i + 1)
                if not (marked and other):
                    raise ValueError('crossfade %s: both overlapping zones must carry ~' % names)
                if not ((lo < vlo and hi < vhi) or (lo > vlo and hi > vhi)):
                    raise ValueError('crossfade %s: equal or nested velocity intervals have no fade direction' % names)
                pairs.setdefault((c, i), []).append(j)
                pairs.setdefault((c, j), []).append(i)
        for i, (lo, hi, marked) in enumerate(bounds):
            if marked:
                maximum = 0
                for j in pairs.get((c, i), ()):
                    vlo, vhi, _ = bounds[j]
                    left, right = max(lo, vlo), min(hi, vhi)
                    maximum = max(maximum, crossfade_attenuation(left, left, right, lo > vlo),
                                  crossfade_attenuation(right, left, right, lo > vlo))
                maxima[(c, ('z', i))] = maximum
    return pairs, maxima


def patch_spec(cfg, key):
    """Resolve an emitted patch identity without creating or allocating its voices."""
    c, sub = key
    if sub is None:
        rname, kw, stream = cfg.maps[c]
        name = 'ch%d' % (c + 1)
    elif isinstance(sub, int):
        rname, kw = cfg.programs[sub]
        stream, name = cfg.maps[c][2], 'ch%dp%d' % (c + 1, sub)
    elif sub[0] == 'z':
        _, _, rname, kw, stream = cfg.zones[c][sub[1]]
        name = 'ch%dz%d' % (c + 1, sub[1] + 1)
    else:
        rname, kw, _ = cfg.kits[c][sub[1]]
        stream, name = 'ch%d' % (c + 1), 'ch%dk%d' % (c + 1, sub[1])
    return name, rname, kw, stream


def configured_pool(cfg, key):
    c = key[0]
    name, rname, kw, stream = patch_spec(cfg, key)
    crossfade = ((cfg.crossfades[key], tuple(cfg.ctls.get(c, {}).values()))
                 if key in cfg.crossfades else None)
    pool = Pool(name, rname, kw, 1 if c in cfg.monos else cfg.voices,
                cfg.filters.get(c), crossfade, cfg.fs)
    pool.stream = stream
    return pool


def parse_pool_entries(values, option, names):
    parsed = {}
    for words in entries(values):
        m = re.fullmatch(r'([A-Za-z0-9_.-]+)=([0-9]+)', words[0])
        if len(words) != 1 or m is None or not 0 <= int(m[2]) <= 64:
            raise ValueError('--%s takes PATCH=INTEGER0..64' % option)
        name, value = m[1], int(m[2])
        if name not in names:
            raise ValueError('--%s: unknown emitted patch %s' % (option, name))
        if name in parsed:
            raise ValueError('--%s: patch %s is given twice' % (option, name))
        parsed[name] = value
    return parsed


def check_streams(maps, zones, kits):
    """Each stream belongs to one channel, and no two names differ only in case."""
    seen = {}                                          # a stream name, case-folded -> (channel, name)
    for c in sorted(set(maps) | set(zones) | set(kits)):
        names = ([maps[c][2]] if c in maps else []) + [z[4] for z in zones.get(c, ())]
        for s in dict.fromkeys(names + (['ch%d' % (c + 1)] if c in kits else [])):
            if s.casefold() in seen:
                c0, s0 = seen[s.casefold()]
                if c0 == c:
                    raise ValueError("--map: channel %d has streams %s and %s, which differ only in case: each"
                                     " stream is a file, and a case-insensitive file system (macOS's default)"
                                     " takes %s.tl and %s.tl for one" % (c + 1, s0, s, s0, s))
                raise ValueError('--map: channels %d and %d share stream %s' % (c0 + 1, c + 1, s) if s == s0 else
                                 "--map: channels %d and %d have streams %s and %s, which differ only in case: each"
                                 " stream is a file, and a case-insensitive file system (macOS's default) takes"
                                 " %s.tl and %s.tl for one" % (c0 + 1, c + 1, s0, s, s0, s))
            seen[s.casefold()] = (c, s)


def parse_programs(values):
    if not values:
        return None
    programs = {}
    for words in entries(values):
        m = PROGRAM_RE.match(words[0])
        if not m or not 1 <= int(m.group(1)) <= 128 or int(m.group(1)) in programs:
            raise ValueError('--program %s: not PROG=RECIPE with PROG 1 to 128, once' % words[0])
        programs[int(m.group(1))] = (m.group(2), recipe_words(m.group(2), words[1:], '--program ' + words[0]))
    return programs


def parse_filters(values):
    filters = {}
    for words in entries(values):
        m = FILTER_RE.match(words[0])
        ch = int(m.group(1)) if m else 0
        if not m or len(words) > 1 or not 1 <= ch <= 16 or ch - 1 in filters:
            raise ValueError('--filter %s: not CH=MODE:HZ:Q (MODE lp, bp, hp or notch), once a channel'
                             % ' '.join(words))
        cm, qm = compose.millihz(Fraction(m.group(3))), round(Fraction(m.group(4)) * 1000)
        if not 1 <= cm <= 0xFFFFFFFF or not 1 <= qm <= 0xFFFF:
            raise ValueError('--filter %s: HZ above 0 and below 4294967.296, Q above 0 and below 65.536' % words[0])
        filters[ch - 1] = (m.group(2), cm, qm)
    return filters


def parse_tuning(path):
    with open(path, encoding='utf-8') as f:
        words = [w for line in f for w in line.split('#', 1)[0].split()]
    if len(words) != 128 or not all(re.match(r'[0-9]+\Z', w) and 1 <= int(w) <= 0xFFFFFFFF for w in words):
        raise ValueError('--tuning %s: not 128 integers from 1 to 4294967295' % os.path.basename(path))
    return [int(w) for w in words]


def channel_of(text, option, entry, mapped):
    ch = int(text)
    if not 1 <= ch <= 16 or ch - 1 not in mapped:
        raise ValueError('%s %s: channel %d is not 1 to 16 with --map or --kit' % (option, entry, ch))
    return ch - 1


def cents_of(text, option, entry, top):
    cents = Fraction(text)
    if cents > top:
        raise ValueError('%s %s: CENTS %s is above %d' % (option, entry, text, top))
    return cents


def parse_kits(values):
    """--kit's entries: {channel: {key: (recipe, deltas, mHz or None)}}; an entry without CH= is on
    the channel of the entry before it in the same --kit."""
    kits = {}
    for v in values or ():
        ch = None
        for words in entries([v]):
            m = KIT_RE.match(words[0])
            if m and m.group(1):
                ch = int(m.group(1))
            if not m or ch is None or not 1 <= ch <= 16 or not 0 <= int(m.group(2)) <= 127:
                raise ValueError('--kit %s: not [CH=]KEY:RECIPE[:HZ] with CH 1 to 16 (the first entry names it)'
                                 ' and KEY 0 to 127' % words[0])
            key, kit = int(m.group(2)), kits.setdefault(ch - 1, {})
            mhz = None if m.group(4) is None else int(Fraction(m.group(4)) * 1000 + Fraction(1, 2))
            if key in kit or mhz is not None and not 1 <= mhz <= 0xFFFFFFFF:
                raise ValueError('--kit %s: key %d listed twice, or HZ not above 0 and below 4294967.2955'
                                 % (words[0], key))
            kit[key] = (m.group(3), recipe_words(m.group(3), words[1:], '--kit ' + words[0]), mhz)
    return kits


def parse_modes(a, maps, zones, kits):
    """--mono, --glide, --unison, --choke and --random: their tables, checked."""
    mapped, monos, glides, unisons, chokes, randoms = set(maps) | set(zones) | set(kits), {}, {}, {}, {}, {}
    for words in entries(a.mono):
        m = MONO_RE.match(words[0])
        if not m or len(words) > 1:
            raise ValueError('--mono %s: not CH=MODE[:PRIORITY], MODE retrigger or legato, PRIORITY last,'
                             ' low or high' % ' '.join(words))
        c = channel_of(m.group(1), '--mono', words[0], mapped)
        if c in monos or c in zones or c in kits:
            raise ValueError('--mono %s: channel %d is given twice, or has a key range or a kit (one patch'
                             ' plays a mono channel)' % (words[0], c + 1))
        monos[c] = (m.group(2), m.group(3) or 'last')
    for words in entries(a.glide):
        m = GLIDE_RE.match(words[0])
        if not m or len(words) > 1 or not 1 <= int(m.group(2)) <= GLIDE_MAX_MS:
            raise ValueError('--glide %s: not CH=MS with MS 1 to %d' % (' '.join(words), GLIDE_MAX_MS))
        c = channel_of(m.group(1), '--glide', words[0], mapped)
        if c not in monos or c in glides:
            raise ValueError('--glide %s: channel %d has no --mono, or is given twice' % (words[0], c + 1))
        glides[c] = int(m.group(2))
    for words in entries(a.unison):
        m = UNISON_RE.match(words[0])
        if not m or len(words) > 1 or not 2 <= int(m.group(2)) <= 8:
            raise ValueError('--unison %s: not CH=N:CENTS with N 2 to 8 and CENTS 0 to 1200, at most two'
                             ' decimals' % ' '.join(words))
        c, n = channel_of(m.group(1), '--unison', words[0], mapped), int(m.group(2))
        if c in unisons or c in monos or n > a.voices:
            raise ValueError('--unison %s: channel %d is given twice or has --mono, or N is above --voices %d'
                             % (words[0], c + 1, a.voices))
        unisons[c] = (n, cents_of(m.group(3), '--unison', words[0], 1200), m.group(3))
    for words in entries(a.choke):
        m = CHOKE_RE.match(words[0])
        keys = [int(k) for k in m.group(2).split('+')] if m else []
        if not m or len(words) > 1 or max(keys) > 127 or len(set(keys)) < len(keys):
            raise ValueError('--choke %s: not CH=KEY+KEY[+KEY...] with keys 0 to 127, each once'
                             % ' '.join(words))
        group = chokes.setdefault(channel_of(m.group(1), '--choke', words[0], mapped), {})
        for k in keys:
            group.setdefault(k, set()).update(set(keys) - {k})
    for words in entries(a.random):
        m = RANDOM_RE.match(words[0])
        if not m or len(words) > 1 or not 1 <= int(m.group(2)) <= 0xFFFFFFFF or int(m.group(4) or 0) > 126:
            raise ValueError('--random %s: not CH=SEED:CENTS[:VEL] with SEED 1 to 4294967295, CENTS 0 to 1200'
                             ' (at most two decimals) and VEL 0 to 126' % ' '.join(words))
        c = channel_of(m.group(1), '--random', words[0], mapped)
        if c in randoms:
            raise ValueError('--random %s: channel %d is given twice' % (words[0], c + 1))
        randoms[c] = (int(m.group(2)), cents_of(m.group(3), '--random', words[0], 1200), int(m.group(4) or 0),
                      m.group(3))
    return monos, glides, unisons, chokes, randoms


def parse_grooves(a, mapped, kits, monos, mpe, scales, ctls):
    """regression opt-in arp/chord spelling and cross-mode refusals, before conversion."""
    arps, chords, latches, seeds = {}, {}, set(), {}
    for option, target in (('arp', arps), ('chord', chords)):
        for words in entries(getattr(a, option)):
            entry = ' '.join(words)
            parts = words[0].split('=', 1)
            if len(words) != 1 or len(parts) != 2 or not re.fullmatch('[0-9]+', parts[0]):
                raise ValueError('--%s %s: expected CH=MODE:RATE[:OCT[:GATE]] or CH=SHAPE[:STRUM_MS]'
                                 % (option, entry))
            c = channel_of(parts[0], '--' + option, entry, mapped)
            if c in target:
                raise ValueError('--%s channel %d is given twice' % (option, c + 1))
            fields = parts[1].split(':')
            if option == 'arp':
                modes = ('up', 'down', 'updown', 'downup', 'played', 'chord', 'random')
                match = re.fullmatch(r'1/(1|2|4|8|16|32)([dt]?)', fields[1]) if len(fields) >= 2 else None
                if not 2 <= len(fields) <= 4 or fields[0] not in modes or not match:
                    raise ValueError('--arp %s: invalid MODE or RATE (1/1..1/32, optional d/t)' % entry)
                rate = Fraction(1, int(match.group(1))) * {'': 1, 'd': Fraction(3, 2), 't': Fraction(2, 3)}[match.group(2)]
                octaves = int(fields[2]) if len(fields) >= 3 and re.fullmatch('[0-9]+', fields[2]) else 1 if len(fields) < 3 else 0
                try:
                    gate = Fraction(fields[3]) if len(fields) == 4 else Fraction(4, 5)
                except (ValueError, ZeroDivisionError, OverflowError):
                    gate = 0
                if not 1 <= octaves <= 11 or gate <= 0:
                    raise ValueError('--arp %s: OCT is 1..11 and GATE is finite and positive' % entry)
                arps[c] = (fields[0], rate, octaves, gate, fields[1])
            else:
                if not 1 <= len(fields) <= 2 or not re.fullmatch(r'[0-9]+(?:\+[0-9]+){0,7}', fields[0]):
                    raise ValueError('--chord %s: SHAPE needs 1..8 offsets joined by +' % entry)
                shape = tuple(int(x) for x in fields[0].split('+'))
                if shape != tuple(sorted(set(shape))) or shape[0] != 0 or shape[-1] > 127 or compose.chord_shape(shape) != shape:
                    raise ValueError('--chord %s: SHAPE must rise from 0 with distinct MIDI offsets' % entry)
                try:
                    spread = Fraction(fields[1]) * 1000000 if len(fields) == 2 else Fraction(0)
                except (ValueError, ZeroDivisionError, OverflowError):
                    spread = Fraction(-1)
                if spread.denominator != 1 or not 0 <= spread <= 200000000:
                    raise ValueError('--chord %s: STRUM_MS must be 0..200 in whole ns' % entry)
                chords[c] = (shape, int(spread))
    for words in entries(a.arp_latch):
        if len(words) != 1 or not re.fullmatch('[0-9]+', words[0]):
            raise ValueError('--arp-latch expects CH[,CH...]')
        c = channel_of(words[0], '--arp-latch', words[0], mapped)
        if c not in arps or c in latches:
            raise ValueError('--arp-latch channel %d has no --arp or is given twice' % (c + 1))
        latches.add(c)
    for words in entries(a.arp_seed):
        match = re.fullmatch(r'([0-9]+)=([0-9]+)', words[0])
        if len(words) != 1 or not match or int(match.group(2)) > 0xffffffff:
            raise ValueError('--arp-seed expects CH=UINT32')
        c = channel_of(match.group(1), '--arp-seed', words[0], mapped)
        if c not in arps or c in seeds:
            raise ValueError('--arp-seed channel %d has no --arp or is given twice' % (c + 1))
        seeds[c] = int(match.group(2))
    zones = {ch for master, members in mpe.items() for ch in (master,) + members}
    for c in arps.keys() | chords.keys():
        option = '--arp' if c in arps else '--chord'
        if c in arps and c in chords:
            raise ValueError('--arp and --chord both name channel %d' % (c + 1))
        for name, group in (('--scale', scales), ('--kit', kits), ('--mono', monos), ('--mpe', zones)):
            if c in group:
                raise ValueError('%s channel %d cannot combine with %s' % (option, c + 1, name))
        if c in arps and ('relvel',) in ctls.get(c, {}):
            raise ValueError('--arp channel %d cannot combine with --ctl relvel: gates have no physical release velocity' % (c + 1))
    return arps, chords, latches, seeds


def parse_arp_conditions(a, mapped, arps):
    """ explicit condition indices and already-processed neighbor channels."""
    conditions, neighbors = {}, {}
    for words in entries(a.arp_cond):
        match = re.fullmatch(r'([0-9]+)=([0-9]+):(pre|not-pre|nei)', words[0])
        if len(words) != 1 or not match:
            raise ValueError('--arp-cond expects CH=STEP:pre|not-pre|nei')
        c = channel_of(match.group(1), '--arp-cond', words[0], mapped)
        g = int(match.group(2))
        if c not in arps or g in conditions.get(c, {}):
            raise ValueError('--arp-cond channel %d needs --arp and each STEP once' % (c + 1))
        conditions.setdefault(c, {})[g] = match.group(3)
    for words in entries(a.arp_nei):
        match = re.fullmatch(r'([0-9]+)=([0-9]+)', words[0])
        if len(words) != 1 or not match:
            raise ValueError('--arp-nei expects CH=SOURCE_CH')
        c = channel_of(match.group(1), '--arp-nei', words[0], mapped)
        source = channel_of(match.group(2), '--arp-nei', words[0], mapped)
        if c not in arps or source not in arps or source >= c or c in neighbors:
            raise ValueError('--arp-nei channel %d needs one lower-numbered mapped arp source' % (c + 1))
        neighbors[c] = source
    for c, rows in conditions.items():
        if 'nei' in rows.values() and c not in neighbors:
            raise ValueError('--arp-cond channel %d: nei needs --arp-nei source' % (c + 1))
    return conditions, neighbors


def ctl_source(body, entry):
    """One --ctl entry's SOURCE:N as (source, N): source ('cc', N), ('cc+', N), ('nrpn', M, L),
    ('pressure',), ('polyat',), ('relvel',) or ('rand', SEED)."""
    text, _, n = body.rpartition(':')
    m = CTL_SRC_RE.match(text)
    if not m or len(n) != 1 or n not in '01234567':
        raise ValueError('--ctl %s: not [CH=]SOURCE:N with N 0 to 7 and SOURCE ccN, ccN+, nrpnM.L, pressure,'
                         ' polyat, relvel or rand:SEED, or CH=default' % entry)
    cc, wide, msb, lsb, word, seed = m.groups()
    if cc is not None:
        if not 1 <= int(cc) <= (31 if wide else 119) or int(cc) in CTL_REFUSED:
            raise ValueError('--ctl %s: ccN takes N 1 to 119 and ccN+ N 1 to 31, not 6, 38, 64 or 96 to 101'
                             ' (data entry, the pedal, the RPN and NRPN selects)' % entry)
        return ('cc+' if wide else 'cc', int(cc)), int(n)
    if msb is not None:
        if int(msb) > 127 or int(lsb) > 127 or (int(msb), int(lsb)) == (127, 127):
            raise ValueError('--ctl %s: nrpnM.L takes M and L 0 to 127, not both 127 (the null parameter)'
                             % entry)
        return ('nrpn', int(msb), int(lsb)), int(n)
    if word:
        return (word,), int(n)
    if not 1 <= int(seed) <= 0xFFFFFFFF:
        raise ValueError('--ctl %s: rand:SEED takes SEED 1 to 4294967295' % entry)
    return ('rand', int(seed)), int(n)


def ctl_name(source):
    """A --ctl source as the option spells it."""
    if source[0] in ('cc', 'cc+'):
        return 'cc%d%s' % (source[1], '+' if source[0] == 'cc+' else '')
    if source[0] == 'nrpn':
        return 'nrpn%d.%d' % source[1:]
    return 'rand:%d' % source[1] if source[0] == 'rand' else source[0]


def ctl_claims(source):
    """The CC numbers a --ctl source reads its value from."""
    return {source[1]} if source[0] == 'cc' else {source[1], source[1] + 32} if source[0] == 'cc+' else set()


def ctl_default(c):
    """--ctl CH=default, the fixed assignment of : ctl0 the mod wheel, ctl1 channel and poly
    pressure, ctl2 breath, ctl3 expression, ctl5 release velocity and ctl6 the per-note random value,
    seeded by 2654435769 (0x9E3779B9) times the channel's number, modulo 2^32, which is never 0
    (provisional); ctl4, MPE slide, waits for --mpe, and ctl7 is free."""
    return [(('cc', 1), 0), (('pressure',), 1), (('polyat',), 1), (('cc', 2), 2), (('cc', 11), 3),
            (('relvel',), 5), (('rand', 0x9E3779B9 * (c + 1) & 0xFFFFFFFF), 6)]


def parse_ctls(values, mapped, filters, monos):
    """--ctl's entries: {channel: {source: input}}; an entry without CH= is on the channel of the
    entry before it in the same --ctl."""
    ctls = {}
    for v in values or ():
        ch = None
        for words in entries([v]):
            m = CTL_RE.match(words[0])
            if m and m.group(1):
                ch = m.group(1)
            if not m or ch is None or len(words) > 1:
                raise ValueError('--ctl %s: not [CH=]SOURCE:N or CH=default (the first entry names CH)'
                                 % ' '.join(words))
            c = channel_of(ch, '--ctl', words[0], mapped)
            if c in monos:
                raise ValueError('--ctl %s: channel %d has --mono, which --ctl does not take yet' % (words[0], c + 1))
            table = ctls.setdefault(c, {})
            for source, n in ctl_default(c) if m.group(2) == 'default' else [ctl_source(m.group(2), words[0])]:
                rand, claimed = source[0] == 'rand', set().union(*map(ctl_claims, table))
                if (source in table or rand and any(s[0] == 'rand' for s in table) or ctl_claims(source) & claimed
                        or source == ('cc', 74) and c in filters
                        or any((s[0] == 'rand') != rand and k == n for s, k in table.items())):
                    raise ValueError('--ctl %s: on channel %d, %s is mapped twice, reads a CC already mapped'
                                     ' (ccN+ reads N and N + 32), is CC 74 on a channel with --filter, or'
                                     ' shares ctl%d with rand, which takes its input alone'
                                     % (words[0], c + 1, ctl_name(source), n))
                table[source] = n
    return ctls


def parse_mpe(values, maps, zones, kits, modes, ctls):
    """--mpe's zones (the docstring's MPE): {master: members}, the members from the master outward,
    checked against the other options; modes are the --mono, --unison, --choke and --random tables."""
    mpe = {}
    for words in entries(values):
        m = MPE_RE.match(words[0])
        if not m or len(words) > 1 or not 1 <= int(m.group(2)) <= 15:
            raise ValueError('--mpe %s: not lower:N or upper:N with N 1 to 15 members' % ' '.join(words))
        master, n = (0 if m.group(1) == 'lower' else 15), int(m.group(2))
        if master in mpe:
            raise ValueError('--mpe %s: the %s zone is given twice' % (words[0], m.group(1)))
        mpe[master] = tuple(range(1, n + 1)) if master == 0 else tuple(range(14, 14 - n, -1))
    if len(mpe) == 2 and {0, *mpe[0]} & {15, *mpe[15]}:
        raise ValueError('--mpe: the lower zone (members channels 2 to %d) and the upper zone (members channels'
                         ' %d to 15) overlap: their members come to 14 at most' % (mpe[0][-1] + 1, mpe[15][-1] + 1))
    for master, members in sorted(mpe.items()):
        if master not in maps:
            raise ValueError('--mpe: the zone of channel %d needs a plain --map %d=RECIPE entry, its one patch'
                             % (master + 1, master + 1))
        if master in zones or any(master in table for table in modes):
            raise ValueError('--mpe: channel %d, a zone master, has a key range, --mono, --unison, --choke or'
                             ' --random; a zone plays one patch' % (master + 1))
        for c in members:
            if c in maps or c in zones or c in kits:
                raise ValueError('--mpe: channel %d, a member of the zone of channel %d, has its own --map or --kit'
                                 % (c + 1, master + 1))
        if any(n in (1, 4) for n in ctls.get(master, {}).values()):
            raise ValueError("--mpe: --ctl on channel %d maps a source to ctl1 or ctl4, which the zone's member"
                             " pressure and slide take (so not CH=default)" % (master + 1))
    return mpe


def scala_lines(path, option):
    """The lines of a Scala file that are not comments (! first): [(line number, text)]."""
    try:
        with open(path, encoding='latin-1') as f:
            return [(k, line.strip()) for k, line in enumerate(f, 1) if not line.startswith('!')]
    except OSError as e:
        raise ValueError('%s %s: %s' % (option, shown(path), e.strerror)) from None


def scala_number(text, where, ratio=False):
    """A .scl pitch (cents when it holds a period, else a ratio N or N/D: a Fraction, or a Decimal
    for cents), or with ratio False a .kbm integer; ValueError names where."""
    word = text.split()[0] if text.split() else ''
    if ratio and '.' in word and re.match(r'[-+]?[0-9]*\.[0-9]*\Z', word) and word.strip('+-.'):
        return decimal.Decimal(word)
    if ratio and re.match(r'[0-9]+(/[0-9]+)?\Z', word) and int(word.split('/')[0]) > 0 and \
            int((word + '/1').split('/')[1]) > 0:
        return Fraction(word)
    if not ratio and re.match(r'[0-9]+\Z', word):
        return int(word)
    raise ValueError('%s: %r is not %s' % (where, text, 'a pitch (cents with a period, or a ratio N or N/D'
                                           ' above 0)' if ratio else 'a whole number'))


def scala_files(value):
    """--scala's value as (the .scl, the .kbm or None): the part after the last colon is the .kbm
    when it ends in .kbm, in any case."""
    scl, colon, kbm = value.rpartition(':')
    return (scl, kbm) if colon and kbm.lower().endswith('.kbm') else (value, None)


def parse_scala(value, scale=False):
    """--scala FILE.scl[:FILE.kbm]: the 128-entry millihertz table of the docstring's Scala, None
    for a key it leaves unmapped."""
    scl, kbm = scala_files(value)
    with decimal.localcontext() as ctx:
        ctx.prec, ctx.rounding = SCALA_PREC, decimal.ROUND_HALF_EVEN
        lines = scala_lines(scl, '--scala')
        name = '--scala %s' % shown(scl)
        body = [(k, t) for k, t in lines[1:] if t]     # past the description, blank lines skipped
        if not lines or not body:
            raise ValueError('%s: line %d: no description and note count' % (name, lines[-1][0] if lines else 1))
        n = scala_number(body[0][1], '%s: line %d' % (name, body[0][0]))
        if n < 1 or len(body) < n + 1:
            raise ValueError('%s: line %d: %d notes, and %s follow' % (name, body[0][0], n,
                                                                  count(len(body) - 1, 'pitch line')))
        pitches = [scala_number(t, '%s: line %d' % (name, k), True) for k, t in body[1:n + 1]]
        if kbm is None:
            size, first, last, middle, ref, mapping, octave = 0, 0, 127, 60, 60, [], n
            ref_mhz = 440000 * (decimal.Decimal(2) ** (decimal.Decimal(-9) / 12))   # 12-TET middle C
        else:
            name = '--scala %s' % shown(kbm)
            kl = [(k, t) for k, t in scala_lines(kbm, '--scala') if t]
            if len(kl) < 7:
                raise ValueError('%s: line %d: fewer than the 7 header lines' % (name, kl[-1][0] if kl else 1))
            size, first, last, middle, ref = [scala_number(t, '%s: line %d' % (name, k)) for k, t in kl[:5]]
            if scale and size != 12:
                raise ValueError('--scale: %s: line %d: .kbm map size %d is not 12' % (name, kl[0][0], size))
            word = kl[5][1].split()[0]
            if not re.match(r'[0-9]+(\.[0-9]*)?\Z', word) or not Fraction(word):
                raise ValueError('%s: line %d: %r is not a frequency in Hz above 0' % (name, kl[5][0], kl[5][1]))
            ref_mhz = Fraction(word) * 1000
            octave = scala_number(kl[6][1], '%s: line %d' % (name, kl[6][0]))
            for i, (label, key) in enumerate((('first', first), ('last', last), ('middle', middle),
                                              ('reference', ref)), 1):
                if not 0 <= key <= 127:
                    raise ValueError('%s: line %d: %s note %d is not a key 0 to 127' %
                                     (name, kl[i][0], label, key))
            if first > last:
                raise ValueError('%s: line %d: last note %d is before first note %d' %
                                 (name, kl[2][0], last, first))
            mapping = [None if t.split()[0] == 'x' else scala_number(t, '%s: line %d' % (name, k))
                       for k, t in kl[7:7 + size]]
            mapping += [None] * (size - len(mapping))  # entries the file leaves out: unmapped
        def degree(key):
            if not size:
                return key - middle
            q, r = divmod(key - middle, size)
            return None if mapping[r] is None else mapping[r] + q * octave
        def ratio(d):                                  # degree d above the 1/1: periods, then a step
            q, r = divmod(d, n)
            period, step = pitches[-1], pitches[r - 1] if r else Fraction(1)
            if isinstance(period, Fraction) and isinstance(step, Fraction):
                return period ** q * step
            return dec(as_ratio(period)) ** q * dec(as_ratio(step))
        if degree(ref) is None:
            raise ValueError('%s: the reference note %d is unmapped' % (name, ref))
        table, base = [], ratio(degree(ref))
        for key in range(128):
            d = None if not first <= key <= last else degree(key)
            if d is None:
                table.append(None)
                continue
            r = ratio(d)
            if isinstance(r, Fraction) and isinstance(base, Fraction) and isinstance(ref_mhz, Fraction):
                x = ref_mhz * r / base
                f = int(x + Fraction(1, 2))            # round half up, exact
            else:
                x = dec(ref_mhz) * dec(r) / dec(base)
                f = int(x.quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP))
            table.append(f if 1 <= f <= 0xFFFFFFFF else None)   # past 32 bits: unmapped
    return table


def as_ratio(x):
    """A .scl pitch as a ratio: a Fraction as it is, cents c as 2^(c/1200) in the context's decimals."""
    if isinstance(x, Fraction):
        return x
    return (decimal.Decimal(2).ln() * x / 1200).exp()


def dec(x):
    return x if isinstance(x, decimal.Decimal) else decimal.Decimal(x.numerator) / decimal.Decimal(x.denominator)


def parse_scales(values, mapped, kits, monos, mpe):
    """--scale's per-channel locks, resolved by the composer's single scale table."""
    scales, zone_channels = {}, {c for m, cs in mpe.items() for c in (m,) + cs}
    for words in entries(values):
        match = re.fullmatch(r'([0-9]+)=([^:\s]+)(?::([0-9]+))?', words[0])
        if len(words) != 1 or match is None:
            raise ValueError('--scale %s: not CH=NAME|MASK[:ROOT]' % ' '.join(words))
        c, scale, root = int(match[1]) - 1, match[2], int(match[3] or 0)
        if not 0 <= c < 16:
            raise ValueError('--scale: channel %d is not 1 to 16' % (c + 1))
        if c in scales:
            raise ValueError('--scale: channel %d is given twice' % (c + 1))
        for channels, reason in ((kits, 'a --kit channel'), (monos, 'a --mono channel'),
                                  (zone_channels, 'an --mpe zone channel')):
            if c in channels:
                raise ValueError('--scale: channel %d is %s' % (c + 1, reason))
        if c not in mapped:
            raise ValueError('--scale: channel %d has no --map' % (c + 1))
        try:
            scales[c] = compose.scale_mask((int(scale) if re.fullmatch(r'[0-9]+', scale) else scale, root))
        except ValueError as e:
            raise ValueError('--scale %s: %s' % (words[0], e)) from None
    return scales


def config(a):
    """The Config of parsed arguments a; ValueError says what is wrong."""
    maps, zones, velocity_zones, map_order = parse_maps(a.map, detailed=True)
    filters = parse_filters(a.filter)
    fade_pairs, crossfades = fade_overlaps(zones, velocity_zones)
    kits = parse_kits(a.kit)
    for c in kits:
        if c in maps or c in zones:
            raise ValueError('--kit: channel %d also has --map; a kit channel plays its kit alone' % (c + 1))
    check_streams(maps, zones, kits)
    for c in filters:
        if c not in maps and c not in zones and c not in kits:
            raise ValueError('--filter: channel %d has no --map' % (c + 1))
    if not 1 <= a.voices <= 64:
        raise ValueError('--voices %d is not 1 to 64' % a.voices)
    if a.pool is not None and not 1 <= a.pool <= 64:
        raise ValueError('--pool %d is not 1 to 64' % a.pool)
    if a.pool is None and (a.reserve or a.limit):
        raise ValueError('--reserve and --limit require --pool')
    if not NUMBER_RE.match(a.bend) or Fraction(a.bend) > 127:
        raise ValueError('--bend %s is not 0 to 127 semitones' % a.bend)
    if a.tail is not None and not 1 <= a.tail <= 3600000:
        raise ValueError('--tail %d is not 1 to 3600000 ms' % a.tail)
    if a.name is not None and not NAME_RE.match(a.name):
        raise ValueError('--name %s is not 1 to 31 letters, digits, _ . - (not led by . or -)' % a.name)
    monos, glides, unisons, chokes, randoms = parse_modes(a, maps, zones, kits)
    if a.scala and a.tuning:
        raise ValueError('--scala and --tuning each give the tuning table: give one')
    if a.fs is not None and a.fs not in (44100, 48000):
        raise ValueError('--fs %d is not 48000 or 44100' % a.fs)
    options = (['--map ' + v for v in a.map or ()] + ['--program ' + v for v in a.program or ()] +
               ['--filter ' + v for v in a.filter or ()] + ['--voices %d' % a.voices, '--bend ' + a.bend] +
               (['--tuning ' + shown(a.tuning)] if a.tuning else []) +
               (['--tail %d' % a.tail] if a.tail is not None else []) +
               ['--%s %s' % (k, v) for k in ('mono', 'glide', 'unison', 'kit', 'choke', 'random')
                for v in getattr(a, k) or ()] +
               (['--scala ' + ':'.join(shown(f) for f in scala_files(a.scala) if f)] if a.scala else []) +
               (['--rpn'] if a.rpn else []) + ['--ctl ' + v for v in a.ctl or ()] +
               ['--mpe ' + v for v in a.mpe or ()] + (['--fs %d' % a.fs] if a.fs is not None else []) +
               ['--scale ' + v for v in a.scale or ()] +
               ['--%s %s' % (name.replace('_', '-'), v) for name in ('arp', 'chord', 'arp_latch', 'arp_seed', 'arp_cond', 'arp_nei')
                for v in getattr(a, name) or ()] +
               (['--pool %d' % a.pool] if a.pool is not None else []) +
               ['--%s %s' % (name, v) for name in ('reserve', 'limit') for v in getattr(a, name) or ()])
    ctls = parse_ctls(a.ctl, set(maps) | set(zones) | set(kits), filters, monos)
    mpe = parse_mpe(a.mpe, maps, zones, kits, (monos, unisons, chokes, randoms), ctls)
    if a.pool is not None and mpe:
        raise ValueError('--pool with MPE is held by ')
    scales = parse_scales(a.scale, set(maps) | set(zones), kits, monos, mpe)
    arps, chords, arp_latches, arp_seeds = parse_grooves(a, set(maps) | set(zones) | set(kits), kits, monos, mpe, scales, ctls)
    arp_conditions, arp_neighbors = parse_arp_conditions(a, set(maps) | set(zones) | set(kits), arps)
    tuning = parse_tuning(a.tuning) if a.tuning else parse_scala(a.scala, bool(scales)) if a.scala else None
    programs = parse_programs(a.program)
    patch_order = []
    for key in map_order:
        patch_order.append(key)
        if key[1] is None:
            patch_order += [(key[0], prog) for prog in programs or {}]
    patch_order += [(c, ('k', key)) for c in sorted(kits) for key in sorted(kits[c])]
    cfg = Config(maps=maps, programs=programs, filters=filters, voices=a.voices,
                  bend=Fraction(a.bend), tuning=tuning, tail_ms=a.tail, options=options, zones=zones,
                  kits=kits, monos=monos, glides=glides, unisons=unisons, chokes=chokes, randoms=randoms,
                  rpn=a.rpn, ctls=ctls, mpe=mpe, zone_of={c: m for m, cs in mpe.items() for c in (m,) + cs},
                  fs=a.fs or FS, scales=scales, arps=arps, chords=chords, arp_latches=arp_latches, arp_seeds=arp_seeds,
                  arp_conditions=arp_conditions, arp_neighbors=arp_neighbors,
                  velocity_zones=velocity_zones, fade_pairs=fade_pairs, crossfades=crossfades,
                  pool_total=a.pool, patch_order=patch_order)
    names = {patch_spec(cfg, key)[0]: key for key in patch_order}
    cfg.reserves = parse_pool_entries(a.reserve, 'reserve', names)
    cfg.limits = parse_pool_entries(a.limit, 'limit', names)
    for name, value in cfg.limits.items():
        if names[name][0] in monos and value > 1:
            raise ValueError('--limit %s: a mono patch takes at most1 voice' % name)
    for name, reserve in cfg.reserves.items():
        limit = cfg.limits.get(name, 1 if names[name][0] in monos else a.voices)
        if reserve > limit:
            raise ValueError('--reserve %s=%d exceeds limit%d' % (name, reserve, limit))
    if a.pool is not None and sum(cfg.reserves.values()) > a.pool:
        raise ValueError('--reserve total exceeds --pool%d' % a.pool)
    for key in crossfades:
        configured_pool(cfg, key)          # preflight unused definitions too
    return cfg


def count(n, word):
    return '%d %s%s' % (n, word, '' if n == 1 else ('es' if word.endswith('ch') else 's'))


def secs(ns):
    return '%d.%09d' % divmod(ns, 10 ** 9)


def shown(path):
    """A file's name as a record shows it: the basename, printable ASCII only."""
    return re.sub(r'[^ -~]', '?', os.path.basename(path))


def inside(path, root):
    return os.path.commonpath([path, root]) == root


def music_parts(path):
    """The names leading from music down to path, or None when path does not lie in it. The
    file system decides, not the spelling: links are resolved, and each existing directory on the
    way up is compared with music by os.path.samefile, so a case variant (one directory on
    a case-insensitive file system; os.path.normcase does not fold case on macOS) is caught."""
    parts, d = [], os.path.realpath(path)
    while True:
        if os.path.exists(d) and os.path.samefile(d, compose.MUSIC):
            return parts
        d, tail = os.path.split(d)
        if not tail:
            return None
        parts.insert(0, tail)


def rel(path):
    """path relative to the repository root when it lies inside it."""
    return os.path.relpath(path, ROOT) if inside(path, ROOT) else path


def default_name(path):
    """A piece name from the file's name: each run of other characters made one -, 31 at most."""
    name = re.sub(r'[^A-Za-z0-9_.-]+', '-', os.path.splitext(os.path.basename(path))[0]).lstrip('.-')[:31]
    return name if NAME_RE.match(name) else 'midi'


def parser(*, allow_abbrev=True):
    ap = argparse.ArgumentParser(prog='midi2tl.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=allow_abbrev)
    ap.add_argument('midi', nargs='?', help='the Standard MIDI File, format 0 or 1')
    ap.add_argument('--map', action='append', metavar='CH=RECIPE[:STREAM][@LO-HI[/VLO-VHI[~]]] [ARG=DELTA ...]')
    ap.add_argument('--program', action='append', metavar='PROG=RECIPE [ARG=DELTA ...]')
    ap.add_argument('--filter', action='append', metavar='CH=MODE:HZ:Q')
    ap.add_argument('--voices', type=int, default=8, metavar='N')
    ap.add_argument('--pool', type=int, metavar='N')
    ap.add_argument('--reserve', action='append', metavar='PATCH=N[,PATCH=N...]')
    ap.add_argument('--limit', action='append', metavar='PATCH=N[,PATCH=N...]')
    ap.add_argument('--bend', default='2', metavar='ST')
    ap.add_argument('--tuning', metavar='FILE')
    ap.add_argument('--tail', type=int, metavar='MS')
    ap.add_argument('--mono', action='append', metavar='CH=MODE[:PRIORITY]')
    ap.add_argument('--glide', action='append', metavar='CH=MS')
    ap.add_argument('--unison', action='append', metavar='CH=N:CENTS')
    ap.add_argument('--kit', action='append', metavar='CH=KEY:RECIPE[:HZ] [ARG=DELTA ...]')
    ap.add_argument('--choke', action='append', metavar='CH=KEY+KEY[+KEY...]')
    ap.add_argument('--random', action='append', metavar='CH=SEED:CENTS[:VEL]')
    ap.add_argument('--scala', metavar='FILE.scl[:FILE.kbm]')
    ap.add_argument('--scale', action='append', metavar='CH=NAME|MASK:ROOT')
    ap.add_argument('--arp', action='append', metavar='CH=MODE:RATE[:OCT[:GATE]]')
    ap.add_argument('--chord', action='append', metavar='CH=SHAPE[:STRUM_MS]')
    ap.add_argument('--arp-latch', action='append', metavar='CH[,CH...]')
    ap.add_argument('--arp-seed', action='append', metavar='CH=UINT32')
    ap.add_argument('--arp-cond', action='append', metavar='CH=STEP:COND')
    ap.add_argument('--arp-nei', action='append', metavar='CH=SOURCE_CH')
    ap.add_argument('--rpn', action='store_true')
    ap.add_argument('--ctl', action='append', metavar='CH=SOURCE:N | CH=default')
    ap.add_argument('--mpe', action='append', metavar='lower:N | upper:N')
    ap.add_argument('--fs', type=int, metavar='HZ')
    ap.add_argument('--name')
    ap.add_argument('--out', metavar='DIR')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    return ap


AGENT_OPTIONS_NOTE = '''
Agent adapter: pass existing musical options as an argv list, for example
["--map", "1=epiano", "--chord", "1=0+4+7:20", "--name", "study"].
Each value is one list element; do not include shell quotes or split a map's
recipe deltas into separate elements. Repeated options remain repeated.
Input MIDI, --out, --dry-run, --selftest and --help are adapter-owned and
cannot appear inside options. The shared midi_import call supplies source,
out and dry_run explicitly. Dry run writes nothing and invokes no native
code. Conversion requires a new output directory and an already-built
selected harness; it never runs make. Output contains record/ authored TLs
and renders/ canonical TL plus WAV. Existing level/voice/controller rules
and held MPE choices still apply. Logging a take is a separate operation.
'''


class AgentError(Exception):
    def __init__(self, message, status=1, **details):
        super().__init__(message)
        self.status, self.details = status, details


def agent_require(condition, message, status=2):
    if not condition:
        raise AgentError(message, status)


def agent_identity(path, role):
    path = Path(path).resolve()
    data = path.read_bytes()
    return dict(role=role, path=str(path), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


def agent_request(text):
    """Strict JSON transport only; musical arguments use the existing parser."""
    def pairs(items):
        value = {}
        for key, item in items:
            agent_require(key not in value, 'duplicate request key: ' + key)
            value[key] = item
        return value
    def constant(value):
        raise AgentError('nonfinite request value: ' + value, 2)
    try:
        request = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except ValueError as error:
        raise AgentError('invalid request JSON: ' + str(error), 2) from error
    allowed = {'source', 'options', 'out', 'dry_run', 'harness'}
    agent_require(isinstance(request, dict) and set(request) <= allowed and 'source' in request,
                  'request needs source and only source/options/out/dry_run/harness keys')
    request = {**dict(options=[], out=None, dry_run=True, harness=None), **request}
    for key in ('source', 'out', 'harness'):
        value = request[key]
        agent_require(key != 'source' and value is None or
                      isinstance(value, str) and bool(value) and '\0' not in value,
                      key + ' must be a nonempty path' + ('' if key == 'source' else ' or null'))
    agent_require(type(request['dry_run']) is bool, 'dry_run must be boolean')
    agent_require(isinstance(request['options'], list) and all(isinstance(value, str) and '\0' not in value
                  for value in request['options']), 'options must be an argv list of strings')
    agent_require(not request['dry_run'] or request['out'] is None and request['harness'] is None,
                  'dry run forbids out and harness')
    agent_require(request['dry_run'] or request['out'] is not None and request['harness'] is not None,
                  'conversion requires out and an already-built harness')
    return request


def agent_convert(request, result):
    """Isolated child adapter over the established converter; never builds."""
    result.update(dry_run=request['dry_run'], options=request['options'])
    try:
        # Exact option names only: an abbreviation must not reach a file option unchecked.
        arguments = parser(allow_abbrev=False).parse_args(request['options'])
    except SystemExit as error:
        raise AgentError('invalid or reserved converter option; read midi-options reference', 2) from error
    agent_require(arguments.midi is None and arguments.out is None and not arguments.dry_run
                  and not arguments.selftest, 'input, out, dry-run and selftest are adapter-owned')
    workspace = os.environ.get('JOTTUNE_MCP_WORKSPACE')
    if workspace:
        selected = [request['source'], request['out'], arguments.tuning,
                    *(scala_files(arguments.scala) if arguments.scala else ())]
        agent_require(all(path is None or Path(path).resolve().is_relative_to(Path(workspace).resolve())
                          for path in selected), 'tool path leaves the configured workspace')
    source = Path(request['source']).resolve()
    name = arguments.name or default_name(str(source))
    destination = None
    if not request['dry_run']:
        raw_destination = Path(request['out']).absolute()
        agent_require(not os.path.lexists(raw_destination), 'MIDI output already exists')
        destination = raw_destination.resolve()
        agent_require(music_parts(str(destination / 'record')) is None,
                      'structured MIDI output cannot be placed inside the music corpus')
    dependencies = [agent_identity(source, 'source'), agent_identity(__file__, 'converter'),
                    agent_identity(compose.__file__, 'composer'),
                    agent_identity(compose.RECIPE_FILE, 'recipes'), agent_identity(compose.TABLE_FILE, 'tables'),
                    agent_identity(Path(ROOT) / 'src/gen/params.def', 'generator_schema')]
    if arguments.tuning:
        dependencies.append(agent_identity(arguments.tuning, 'tuning'))
    if arguments.scala:
        dependencies.extend(agent_identity(path, role) for role, path in
                            zip(('scala', 'kbm'), scala_files(arguments.scala)) if path)
    if destination is not None:
        dependencies.append(agent_identity(request['harness'], 'native'))
    result.update(name=name, source=dependencies[0], dependencies=dependencies,
                  native=next((item for item in dependencies if item['role'] == 'native'), None))
    def unchanged():
        for item in dependencies:
            agent_require(agent_identity(item['path'], item['role']) == item,
                          'MIDI dependency changed during conversion: ' + item['role'], 5)
    # compose imports these before this child receives its request. Reload with
    # its existing readers inside the hashed interval, including macro aliases.
    compose.RECIPES = compose.load_recipes(compose.RECIPE_FILE)
    compose.MACRO_TEMPLATES = compose.RECIPES.templates
    compose.TABLES = compose.load_tables(compose.TABLE_FILE)
    try:
        schema = compose.load_gen_descriptors(Path(ROOT) / 'src/gen/params.def')
    except ValueError as error:
        raise AgentError('generator schema changed or is invalid: ' + str(error), 5) from error
    agent_require(schema == (compose.GEN_KINDS, compose.GEN_DESCRIPTORS),
                  'generator schema changed since child import', 5)
    unchanged()
    cfg = config(arguments)
    conversion = Conversion(source.read_bytes(), cfg)
    totals = Counter()
    for counts in conversion.st:
        totals.update(counts)
    channels = conversion.channels()
    result.update(fs=cfg.fs, end_ns=conversion.end_ns, length_ns=conversion.length_ns(),
                  counts=dict(channels=len(channels), streams=sum(len(conversion.streams_of(c)) for c in channels),
                              patches=sum(len(conversion.pools[c]) for c in channels),
                              voices=sum(conversion.voices(c) for c in channels), statistics=dict(totals)),
                  channels=[dict(channel=c + 1, mapped=cfg.mapped(c), statistics=dict(conversion.st[c]),
                                 ignored_cc=dict(conversion.ccs[c])) for c in range(16)
                            if conversion.st[c] or conversion.ccs[c] or cfg.mapped(c)],
                  midi=conversion.info, normalized_options=cfg.options,
                  summary=conversion.summary(name, str(source)), problems=conversion.problems())
    unchanged()
    agent_require(not result['problems'], '; '.join(result['problems']), 1)
    if request['dry_run']:
        return
    # Reuse the explicit bundle's publication lock. Imports happen only in this
    # child path; the ordinary converter and its module users remain unchanged.
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    import fm
    session = fm.session_module()
    native = result['native']['path']
    saved_harness, saved_run = compose.H, compose.subprocess.run
    def observed_run(command, *args, **kwargs):
        agent_require(isinstance(command, list) and command[:2] == [native, '--play'],
                      'unexpected command during isolated MIDI rendering', 5)
        result['render_attempts'] += 1
        completed = saved_run(command, *args, **kwargs)
        result['native_calls'].append(dict(argv=command, returncode=completed.returncode,
                                           stdout=completed.stdout, stderr=completed.stderr))
        if completed.returncode:
            status = completed.returncode if completed.returncode in (1, 2, 3, 4, 5) else 5
            raise AgentError('native MIDI render failed', status, native_returncode=completed.returncode)
        return completed
    with session.destination_lock(fm, destination):
        agent_require(not os.path.lexists(destination), 'MIDI output already exists')
        staging = Path(tempfile.mkdtemp(prefix='.midi-pending-', dir=destination.parent))
        try:
            compose.H, compose.subprocess.run = native, observed_run
            piece = conversion.piece(name, str(staging / 'record'), str(source))
            line, audio_hash = render(piece, str(staging / 'renders'), True)
            piece.validate_crossfades(piece._final_levels)
            canonical, wav = ('renders/' + name + '-mono' + suffix for suffix in ('.tl', '.wav'))
            artifacts = [dict(path=str(path.relative_to(staging)),
                              bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                         for path in sorted(staging.rglob('*')) if path.is_file()]
            record_artifact = next(item for item in artifacts if item['path'] == canonical)
            wav_artifact = next(item for item in artifacts if item['path'] == wav)
            unchanged()
            agent_require(not os.path.lexists(destination), 'MIDI output already exists')
            os.rename(staging, destination)
            result.update(output=str(destination), artifacts=artifacts,
                          record=record_artifact, wav=wav_artifact, hash=audio_hash, play=line)
        finally:
            compose.H, compose.subprocess.run = saved_harness, saved_run
            if staging.exists():
                shutil.rmtree(staging)


def agent_main(text):
    result = dict(version=1, ok=False, exit=1, operation='midi_import', render_attempts=0,
                  native_calls=[], artifacts=[], output=None, record=None, wav=None)
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            agent_convert(agent_request(text), result)
            result.update(ok=True, exit=0)
        except AgentError as error:
            result.update(exit=error.status, message=str(error), **error.details)
        except (OSError, fm_error_type()) as error:
            result.update(exit=5, message=str(error))
        except (ValueError, MidiError, AssertionError, SystemExit) as error:
            # Existing converter/leveller refusals retain their policy status.
            message = str(error) or 'the converted record and its replay differ'
            status = 5 if message.startswith('harness failed:') else 1
            result.update(exit=status, message=message)
    result['diagnostics'] = dict(stdout=stdout.getvalue(), stderr=stderr.getvalue())
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return result['exit']


def fm_error_type():
    """Avoid importing the host facade for inspect-only or ordinary CLI calls."""
    return sys.modules['fm'].ToolError if 'fm' in sys.modules else OSError


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ['--agent-options']:
        print(parser().format_help() + AGENT_OPTIONS_NOTE, end='')
        return 0
    if argv == ['--agent-json']:
        return agent_main(sys.stdin.read())
    a = parser().parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.midi:
        parser().error('name a MIDI file, or give --selftest')
    try:
        cfg = config(a)
        with open(a.midi, 'rb') as f:
            conv = Conversion(f.read(), cfg)
    except (ValueError, MidiError, OSError) as e:
        print('midi2tl.py: %s' % e, file=sys.stderr)
        return 1
    name = a.name or default_name(a.midi)
    out_dir = os.path.abspath(a.out) if a.out else os.path.join(RENDERS, name)
    print('\n'.join(conv.summary(name, a.midi)))
    problems = conv.problems()
    for problem in problems:
        print('  problem: %s' % problem)
    if a.dry_run:
        print('dry run: nothing written')
        return 1 if problems else 0
    if problems:
        print('midi2tl.py: not converted', file=sys.stderr)
        return 1
    parts = music_parts(out_dir)
    if parts is not None and parts != [name]:
        print('midi2tl.py: --out in music must be tools/music/%s, where play.sh finds it' % name,
              file=sys.stderr)
        return 1
    session = os.path.join(out_dir, 'session.tl')
    if os.path.exists(session):
        with open(session, encoding='utf-8') as f:
            if MARK not in f.read():
                print('midi2tl.py: %s holds a session.tl that this tool did not write; choose another --out'
                      % rel(out_dir), file=sys.stderr)
                return 1
    compose.build()                                          # make rebuilds the harness only when stale
    p = conv.piece(name, out_dir, a.midi)
    try:
        line, hash_ = render_crossfade(p, True) if p.crossfades else render(p, RENDERS, True)
    except (AssertionError, SystemExit, ValueError) as e:     # compose.py stops with sys.exit or assert
        sys.stdout.flush()                                   # the passes it printed come first
        print('midi2tl.py: compose.py could not finish the record: %s'
              % (str(e) or 'the record and its replay differ'), file=sys.stderr)
        return 1
    stale = sorted(f for f in os.listdir(out_dir) if f.endswith('.tl') and os.path.join(out_dir, f) not in p.files())
    if stale:
        print('note: %s also holds %s, not part of this record' % (rel(out_dir), ', '.join(stale)))
    print('record %s' % rel(out_dir))
    print(line)
    print('hash %s %s' % (name, hash_))
    return 0


# ---------------------------------------------------------------------------
# The self-test: small MIDI files built here, with times and lines worked out by hand
def vlq_bytes(n):
    out = [n & 0x7F]
    while n > 0x7F:
        n >>= 7
        out.insert(0, 0x80 | (n & 0x7F))
    return out


def smf_bytes(fmt, div, tracks):
    """A Standard MIDI File of tracks given as lists of (absolute tick, event bytes); an event
    without its status byte uses running status."""
    out = struct.pack('>4sIHHH', b'MThd', 6, fmt, len(tracks), div)
    for track in tracks:
        body, last = [], 0
        for tick, ev in track:
            body += vlq_bytes(tick - last) + list(ev)
            last = tick
        out += struct.pack('>4sI', b'MTrk', len(body)) + bytes(body)
    return out


def meta(kind, text):
    return [0xFF, kind] + vlq_bytes(len(text)) + list(text.encode('ascii'))


def tempo(us):
    return [0xFF, 0x51, 3, (us >> 16) & 0xFF, (us >> 8) & 0xFF, us & 0xFF]


def data14(v):
    return [v & 0x7F, v >> 7]


EOT = [0xFF, 0x2F, 0]
# A: format 0, 480 ticks a quarter note, --voices 2: a steal, a release steal, a tempo change,
# four bends 1 tick (0.52 ms) apart that merge into two, and a note-off the pedal holds.
A_TRACK = [(0, meta(3, 'test a')), (0, tempo(500000)), (0, [0x90, 60, 100]), (240, [64, 100]),
           (480, [67, 100]), (720, [60, 0]), (960, tempo(250000)), (960, [0x80, 64, 64]), (1200, [67, 64]),
           (1440, [0x90, 72, 127]), (1500, [0xE0] + data14(10000)), (1501, data14(12288)),
           (1502, data14(9000)), (1503, data14(4096)), (1600, [0xB0, 64, 127]), (1620, [0x80, 72, 0]),
           (1700, [0xB0, 64, 0]), (1920, EOT)]
A_LINES = ['0 ch1 0 on 261626 25800', '250000000 ch1 1 on 329628 25800', '500000000 ch1 0 off',
           '500000000 ch1 0 on 391995 25800', '1000000000 ch1 1 off', '1125000000 ch1 0 off',
           '1250000000 ch1 1 on 523251 32767', '1281770833 ch1 0 pitch 415305', '1281770833 ch1 1 pitch 554365',
           '1282812500 ch1 0 pitch 369994', '1282812500 ch1 1 pitch 493883', '1385416667 ch1 1 off']
# B: format 1, 96 ticks a quarter note, a tempo track with a change at tick 192; channel 2 with
# SysEx, an escape, a program change at the start and before its second note, CC 74, aftertouch,
# pressure, CC 1 and running status kept across the escape and a marker; channel 10 (unmapped)
# and channel 3 (stream bass) in the third track.
B_TRACKS = [
    [(0, meta(3, 'test b')), (0, tempo(600000)), (0, [0xFF, 0x58, 4, 4, 2, 24, 8]), (192, tempo(400000)),
     (384, EOT)],
    [(0, [0xF0, 5, 0x7E, 0x7F, 9, 1, 0xF7]), (0, [0xC1, 0]), (0, [0xB1, 74, 64]), (0, [0x91, 48, 80]),
     (48, [0xD1, 40]), (48, [0xA1, 48, 30]), (48, [0xB1, 1, 20]), (48, [0xF7, 2, 0xF3, 1]),
     (96, meta(6, 'here')), (96, [74, 80]), (192, [0xC1, 32]), (192, [0x81, 48, 0]), (192, [0x91, 36, 100]),
     (240, [0xB1, 74, 48]), (288, [0x81, 36, 0]), (300, EOT)],
    [(0, [0x99, 36, 100]), (24, [0x89, 36, 0]), (96, [0x92, 40, 90]), (144, [0x82, 40, 0]), (144, EOT)]]
B_CH2 = ['0 ch2 0 on 130813 20640', '600000000 ch2 0 filter 2000000 700', '1200000000 ch2 0 off',
         '1200000000 ch2 1 on 65406 25800', '1200000000 ch2 1 filter 2000000 700',
         '1400000000 ch2 0 filter 500000 700', '1400000000 ch2 1 filter 500000 700', '1600000000 ch2 1 off']
B_BASS = ['600000000 bass 2 on 82407 23220', '900000000 bass 2 off']
# D: format 0, 96 ticks a quarter note, a tempo change at the tick of a note-off and a note-on.
D_TRACK = [(0, tempo(500000)), (0, [0x90, 69, 127]), (48, [0x90, 72, 64]), (96, tempo(250000)),
           (96, [0x80, 69, 0]), (96, [0x90, 76, 1]), (144, [0x80, 72, 0]), (192, [0x80, 76, 0]), (192, EOT)]
D_LINES = ['0 ch1 0 on 440000 32767', '250000000 ch1 1 on 523251 16512', '500000000 ch1 0 off',
           '500000000 ch1 2 on 659255 258', '625000000 ch1 1 off', '750000000 ch1 2 off']
# E: --voices 1, a bend and then a note-on at one tick; the note-on takes the voice whose released
# note the bend moved, and starts bent itself (D4 + 1 = 311127 mHz): the move is dropped.
E_TRACK = [(0, [0x90, 60, 100]), (48, [0x80, 60, 0]), (96, [0xE0] + data14(12288)), (96, [0x90, 62, 100]),
           (144, [0x80, 62, 0]), (192, EOT)]
E_LINES = ['0 ch1 0 on 261626 25800', '250000000 ch1 0 off', '500000000 ch1 0 on 311127 25800',
           '750000000 ch1 0 off']
# F: --voices 3, a chord of five notes at one tick: the fourth and the fifth steal the voices of
# the first and the second, the notes struck first, and the three struck last sound on.
F_TRACK = [(0, [0x90, 60, 100]), (0, [64, 100]), (0, [67, 100]), (0, [70, 100]), (0, [72, 100]),
           (96, [0x80, 60, 0]), (96, [64, 0]), (96, [67, 0]), (96, [70, 0]), (96, [72, 0]), (192, EOT)]
F_LINES = ['0 ch1 0 off', '0 ch1 1 off', '0 ch1 0 on 261626 25800', '0 ch1 1 on 329628 25800',
           '0 ch1 2 on 391995 25800', '0 ch1 0 on 466164 25800', '0 ch1 1 on 523251 25800',
           '500000000 ch1 0 off', '500000000 ch1 1 off', '500000000 ch1 2 off']


class Checks:
    def __init__(self):
        self.n, self.failed = 0, []

    def ok(self, name, cond, detail=''):
        self.n += 1
        if cond:
            print('ok   %s' % name)
        else:
            self.failed.append(name)
            print('FAIL %s%s' % (name, ': %s' % detail if detail else ''))
        return cond

    def eq(self, name, got, want):
        return self.ok(name, got == want, 'got %r, want %r' % (got, want))

    def lines(self, name, got, want):
        k = next((j for j, (g, w) in enumerate(zip(got, want)) if g != w), min(len(got), len(want)))
        return self.ok(name, got == want, 'line %d: got %r, want %r (%d lines, want %d)' % (
            k + 1, got[k] if k < len(got) else None, want[k] if k < len(want) else None, len(got), len(want)))


def cfg_of(*argv):
    return config(parser().parse_args(['x.mid'] + list(argv)))


def stream_lines(p, s):
    """Stream s's event lines as write orders them, blanks squeezed."""
    return [' '.join(('%d %s %d %s' % (t, s, v, text)).split()) for t, _, _, v, text, _ in sorted(p.ev[s])]


def off_first(path):
    """No on before an off of the same voice at the same ns in the timeline file path."""
    kinds = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            w = line.split('#', 1)[0].split()
            if len(w) >= 4:
                kinds.setdefault((int(w[0]), int(w[2])), []).append(w[3])
    return all('off' not in k[k.index('on'):] for k in kinds.values() if 'on' in k)


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def selftest_units(ck):
    ck.eq('a 4-byte variable-length number', read_vlq(b'\xff\xff\xff\x7f', 0, 4, 'x'), (0x0FFFFFFF, 4))
    ck.eq('the self-test writes it back', vlq_bytes(0x0FFFFFFF), [0xFF, 0xFF, 0xFF, 0x7F])
    tm = TempoMap({'tpqn': 480}, [(0, 0, 0, 500001)])
    ck.eq('times at 500001 us and 480 ticks, rounded half up', [tm.ns(k) for k in (1, 2, 3)],
          [1041669, 2083338, 3125006])
    tm = TempoMap({'tpqn': 480}, [(960, 0, 5, 250000), (0, 0, 1, 500000)])
    ck.eq('times across a tempo change', [tm.ns(k) for k in (960, 1200, 1501)],
          [1000000000, 1125000000, 1281770833])
    ck.eq('of two tempos at one tick the last wins',
          TempoMap({'tpqn': 96}, [(0, 0, 0, 400000), (0, 1, 0, 600000)]).ns(96), 600000000)
    info = parse_smf(smf_bytes(0, 0xE728, [[(0, EOT)]]))[0]
    ck.eq('an SMPTE division read', info['smpte'], (25, 40))
    ck.eq('SMPTE 25 frames of 40 ticks', TempoMap(info, []).ns(1), 1000000)
    ck.eq('SMPTE 29.97 frames of 4 ticks', TempoMap({'smpte': (29, 4)}, []).ns(1), 8341667)
    ck.eq('moves merge in 1 ms runs', merged([0, 999999, 1000000, 1500000, 2999999]), [1, 3, 4])
    table = [440000] * 128
    table[60] = 61000
    ck.eq('12-TET, a bend, a tuning table', [note_mhz(69, 0), note_mhz(60, Fraction(1)), note_mhz(60, 1, table)],
          [440000, 277183, 64627])


    ck.eq(': exact half mHz rounds up, either integer parity',
          [compose.millihz(Fraction(2001, 2000)), compose.millihz(Fraction(2003, 2000))], [1001, 1002])
    ck.eq(': below and above half mHz',
          [compose.millihz(decimal.Decimal(x)) for x in ('1.000499999999', '1.000500000001')], [1000, 1001])
    ck.eq(': tuned and fixed pitches round half up after scaling',
          [note_mhz(60, -12, [2001] * 128), pitch_mhz(2001, -1200, 1200)], [1001, 1001])
    ck.eq(': operator fixed/keyscale mHz round half up; Q16 and dB ties stay even',
          [compose.op(ratio=1 + 0.5 / 65536, level=0.25, ks_break=1.0005, fixed=1.0005)[i]
           for i in (0, 1, 9, 13)], [65536, 2, 1001, 1001])
    ck.eq(': filter mHz rounds half up, resonance ties stay even',
          parse_filters(['1=lp:1.0005:1.0005'])[0], ('lp', 1001, 1000))
    p = compose.Piece('numeric-test', 120, 4, [' checks'])
    p.stream('s', 'test')
    p.patch('p', *compose.recipe('epiano'))
    v = p.add_voices('p', 1, 'test')[0]
    p.filter('p', 'lp', 1.0005, 1.0005)
    p.cutoff('s', 0, v, 1.0005, 1.0005)
    ck.eq(': composer filter header and event share half-up mHz',
          [p.filters['p'], p.ev['s'][0][4]], [('lp', 1001, 1000), 'filter     1001  1000'])
    ck.eq(': geometric glide reaches octave midpoint and exact endpoint',
          [glide_mhz(1000, 4000, 1, 2), glide_mhz(1000, 4000, 2, 2), glide_mhz(0, 4000, 1, 2)],
          [2000, 4000, 4000])
    baseline = [compose.mhz_m(Fraction(139, 2)), pitch_mhz(1000000, 3, 16), glide_mhz(1000, 3000, 2, 3),
                compose.recipe('epiano', m_cents=13)[1]]
    with decimal.localcontext(decimal.Context(prec=3, rounding=decimal.ROUND_DOWN)):
        compose.mhz_m.cache_clear()
        changed = [compose.mhz_m(Fraction(139, 2)), pitch_mhz(1000000, 3, 16), glide_mhz(1000, 3000, 2, 3),
                   compose.recipe('epiano', m_cents=13)[1]]
    ck.eq(': pitch arithmetic ignores caller decimal precision and rounding', changed, baseline)


def selftest_a(ck, tmp, have):
    conv = Conversion(smf_bytes(0, 480, [A_TRACK]), cfg_of('--map', '1=epiano', '--voices', '2'))
    p = conv.piece('selftest-a', os.path.join(tmp, 'a'), 'a.mid')
    ck.lines('A: the events of ch1', stream_lines(p, 'ch1'), A_LINES)
    ck.eq('A: length_ns', p.length_ns, 2938000000)
    keys = ('notes', 'steals', 'release_steals', 'bends', 'bends_kept', 'pitch_events', 'pedal_downs',
            'pedal_held', 'orphan_offs')
    ck.eq('A: the counts', dict((k, conv.st[0][k]) for k in keys), dict(zip(keys, (4, 1, 1, 4, 2, 4, 1, 1, 0))))
    p.write()
    ck.ok('A: off before on in the written ch1.tl', off_first(os.path.join(tmp, 'a', 'ch1.tl')))
    with open(os.path.join(tmp, 'a', 'ch1.tl'), encoding='utf-8') as f:
        stream = f.read()
    with open(os.path.join(tmp, 'a', 'session.tl'), encoding='utf-8') as f:
        session = f.read()
    ck.ok('A: the files say what they are', TIMES_NOTE in stream and ' bpm' not in stream and SESSION_NOTE in session
          and MARK in session and 'voice  1 ch1' in session, session)
    if have:
        print('     A: %s' % render(p, os.path.join(tmp, 'renders'), False)[0])


def selftest_b(ck, tmp, have):
    data = smf_bytes(1, 96, B_TRACKS)
    base = ['--map', '2=epiano,3=round-bass:bass', '--filter', '2=lp:1000:0.7']
    for run, extra in (('B1', []), ('B2', ['--program', '1=epiano,33=round-bass'])):
        conv = Conversion(data, cfg_of(*(base + extra)))
        p = conv.piece('selftest-' + run.lower(), os.path.join(tmp, run), 'b.mid')
        ck.lines('%s: the events of ch2' % run, stream_lines(p, 'ch2'), B_CH2)
        ck.lines('%s: the events of bass' % run, stream_lines(p, 'bass'), B_BASS)
        ck.eq('%s: length_ns' % run, p.length_ns, 4125000000)
        st = conv.st[1]
        ck.eq('%s: the counts' % run, [st[k] for k in ('programs', 'cc74', 'cc74_kept', 'filter_events',
                                                        'aftertouch', 'pressure')] + [dict(conv.ccs[1]), conv.st[9]['skipped']],
              [2, 3, 3, 4, 1, 1, {1: 1}, 1])
        ck.eq('%s: what the reader skipped' % run, [conv.info[k] for k in ('sysex', 'crossed', 'tempo_events')],
              [2, 1, 2])
        if run == 'B1':
            ck.eq('B1: patches, filters, voices', [list(p.patches), p.filters, [v[0] for v in p.voices]],
                  [['ch2', 'ch3'], {'ch2': ('lp', 1000000, 700)}, ['ch2', 'ch2', 'ch3']])
            text = '\n'.join(conv.summary('selftest-b1', 'b.mid'))
            ck.ok('B1: the report says what it ignored', '2 program changes ignored (no --program)' in text
                  and 'percussion, unmapped' in text and st['programs_applied'] == 0, text)
        else:
            ck.eq('B2: patches, filters, voices', [list(p.patches), sorted(p.filters), [v[0] for v in p.voices]],
                  [['ch2p1', 'ch2p33', 'ch3'], ['ch2p1', 'ch2p33'], ['ch2p1', 'ch2p33', 'ch3']])
            ck.ok('B2: two programs applied, the second as round-bass', st['programs_applied'] == 2
                  and p.patches['ch2p33'][0] == compose.recipe('round-bass')[0])
        if have:
            print('     %s: %s' % (run, render(p, os.path.join(tmp, 'renders'), False)[0]))


def selftest_d(ck, tmp, have):
    data, cfg = smf_bytes(0, 96, [D_TRACK]), cfg_of('--map', '1=epiano')
    p = Conversion(data, cfg).piece('selftest-d', os.path.join(tmp, 'd1'), 'd.mid')
    ck.lines('D: the events of ch1', stream_lines(p, 'ch1'), D_LINES)
    ck.eq('D: length_ns', p.length_ns, 2188000000)
    conv = Conversion(smf_bytes(0, 96, [E_TRACK]), cfg_of('--map', '1=epiano', '--voices', '1'))
    ck.lines('E: a move at the ns its voice is taken, dropped', stream_lines(
        conv.piece('selftest-e', os.path.join(tmp, 'e'), 'e.mid'), 'ch1'), E_LINES)
    ck.eq('E: the counts', [conv.st[0][k] for k in ('release_steals', 'bends_kept', 'pitch_events', 'moves_dropped')],
          [1, 1, 0, 1])
    if not have:
        return
    line, h = render(p, os.path.join(tmp, 'renders1'), False)
    print('     D: %s' % line)
    p2 = Conversion(data, cfg).piece('selftest-d', os.path.join(tmp, 'd2'), 'd.mid')
    h2 = render(p2, os.path.join(tmp, 'renders2'), False)[1]
    ck.ok('D: converted again, the same hash and the same bytes', h2 == h and all(
        read_bytes(f1) == read_bytes(f2) for f1, f2 in zip(p.files(), p2.files())), '%s and %s' % (h, h2))
    hand = os.path.join(tmp, 'hand')
    os.makedirs(hand)
    shutil.copy(os.path.join(tmp, 'd1', 'session.tl'), hand)
    with open(os.path.join(hand, 'ch1.tl'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('# selftest-d, stream "ch1", written by hand from the times worked out\n')
        f.write(''.join(line + '\n' for line in D_LINES))
    rc, kv, line3 = compose.play([os.path.join(hand, 'session.tl'), os.path.join(hand, 'ch1.tl')])
    ck.ok('D: the lines written by hand play to the same hash', rc == 0 and kv.get('hash') == h
          and kv.get('match') == 'yes', line3)


def selftest_f(ck, tmp):
    conv = Conversion(smf_bytes(0, 96, [F_TRACK]), cfg_of('--map', '1=epiano', '--voices', '3'))
    p = conv.piece('selftest-f', os.path.join(tmp, 'f'), 'f.mid')
    ck.lines('F: a chord of five on three voices keeps the three struck last', stream_lines(p, 'ch1'), F_LINES)
    ck.eq('F: notes, steals, release steals, voices',
          [conv.st[0][k] for k in ('notes', 'steals', 'release_steals')] + [conv.voices(0)], [5, 2, 0, 3])


def selftest_g(ck, tmp):
    """G: 128 programs listed in one --program word, and 32 patches of recipe plucked-bass on one
    channel: every line of the record within the harness's 1022 bytes."""
    track = [(0, [0x90, 60, 100]), (10, [0x80, 60, 0])]
    for prog in range(1, 32):
        track += [(20 * prog, [0xC0, prog - 1]), (20 * prog, [0x90, 60, 100]), (20 * prog + 10, [0x80, 60, 0])]
    programs = ','.join('%d=plucked-bass' % k for k in range(1, 129))
    conv = Conversion(smf_bytes(0, 96, [track + [(700, EOT)]]),
                      cfg_of('--map', '1=plucked-bass', '--program', programs))
    p = conv.piece('selftest-g', os.path.join(tmp, 'g'), 'g.mid')
    p.write()
    widths = [len(line) for f in p.files() for line in read_bytes(f).split(b'\n')]
    ck.ok('G: 32 patches, 128 programs listed: no line of the record over 1022 bytes',
          len(p.patches) == 32 and not conv.problems() and max(widths) <= 1022,
          '%d patches, the longest line %d bytes' % (len(p.patches), max(widths)))


def selftest_faults(ck):
    five = struct.pack('>4sIHHH', b'MThd', 6, 0, 1, 96) + struct.pack('>4sI', b'MTrk', 8)
    bad = (('format 2', smf_bytes(2, 96, [[(0, EOT)]])),
           ('a truncated chunk', smf_bytes(0, 96, [[(0, [0x90, 60, 100]), (96, EOT)]])[:-2]),
           ('a data byte with no running status', smf_bytes(0, 96, [[(0, [60, 100]), (96, EOT)]])),
           ('a 5-byte delta time', five + bytes([0x81, 0x80, 0x80, 0x80, 0x00] + EOT)),
           ('status 0xF3 in a track', smf_bytes(0, 96, [[(0, [0xF3, 1]), (96, EOT)]])),
           ('a tempo of 0', smf_bytes(0, 96, [[(0, tempo(0)), (96, EOT)]])))
    for name, data in bad:
        try:
            parse_smf(data)
            ck.ok('refused: %s' % name, False, 'read without an error')
        except MidiError:
            ck.ok('refused: %s' % name, True)


def selftest_cli(ck, tmp):
    path = os.path.join(tmp, 'a.mid')
    with open(path, 'wb') as f:
        f.write(smf_bytes(0, 480, [A_TRACK]))
    dry, buf = os.path.join(tmp, 'dry'), io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([path, '--map', '1=epiano', '--voices', '2', '--dry-run', '--out', dry])
    ck.ok('a dry run exits 0, counts the steal and writes nothing',
          rc == 0 and 'steals 1,' in buf.getvalue() and not os.path.exists(dry), buf.getvalue())
    ck.eq('--map with a stream name and deltas', cfg_of('--map', '1=epiano:keys m_index=-0.5 c_r=100').maps,
          {0: ('epiano', {'m_index': -0.5, 'c_r': 100}, 'keys')})
    tuning = os.path.join(tmp, 'tuning.txt')
    with open(tuning, 'w', encoding='utf-8', newline='\n') as f:
        f.write('# a test table\n' + ''.join('%d  # key %d\n' % (1000 + k, k) for k in range(128)))
    ck.eq('--tuning read', cfg_of('--map', '1=epiano', '--tuning', tuning).tuning[60], 1060)
    for argv in (['--map', '17=epiano'], ['--map', '1=no-such-recipe'], ['--map', '1=epiano m_nothing=1'],
                 ['--map', '1=epiano,2=epiano:ch1'], ['--map', '1=epiano:session'],
                 ['--map', '1=epiano,2=epiano:CH1'], ['--map', '1=epiano:Session'],
                 ['--map', '1=epiano', '--filter', '2=lp:1000:0.7'], ['--map', '1=epiano', '--bend', '-1'],
                 ['--map', '1=epiano', '--program', '129=epiano'], ['--map', '1=epiano', '--name', 'nl\n'],
                 ['--map', '1=epiano', '--bend', '2\n']):
        shown_argv = ' '.join(argv).replace('\n', '\\n')
        try:
            cfg_of(*argv)
            ck.ok('refused: %s' % shown_argv, False, 'accepted')
        except ValueError:
            ck.ok('refused: %s' % shown_argv, True)
    case, out, err = os.path.join(tmp, 'case'), io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main([path, '--map', '1=epiano:Keys,2=round-bass:keys', '--out', case])
    ck.ok('streams Keys and keys refused before anything is written',
          rc == 1 and 'differ only in case' in err.getvalue() and not os.path.exists(case), err.getvalue())
    padded = cfg_of('--map', '01=epiano')
    conv = Conversion(read_bytes(path), padded)
    ck.eq('--map 01=epiano: stream and patch ch1',
          [padded.maps[0][2], conv.stream_name(0), [p.name for p in conv.pools[0].values()]], ['ch1', 'ch1', ['ch1']])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([path, '--map', '1=epiano,3=flute', '--dry-run', '--out', dry])
    ck.ok('a mapped channel with no notes is reported', rc == 0 and '\n  ch3: mapped, no notes\n' in buf.getvalue()
          and not os.path.exists(dry), buf.getvalue())
    link = os.path.join(tmp, 'link')
    os.symlink(compose.MUSIC, link)
    try:
        variant = os.path.join(os.path.dirname(compose.MUSIC), 'Music')
        same = os.path.exists(variant) and os.path.samefile(variant, compose.MUSIC)
        got = [music_parts(compose.MUSIC), music_parts(os.path.join(compose.MUSIC, 'x', 'y')),
               music_parts(os.path.join(link, 'x')), music_parts(os.path.join(variant, 'x')), music_parts(tmp)]
    finally:
        os.unlink(link)
    ck.eq('music found by the file system: itself, below it, through a link, by a case variant%s'
          % ('' if same else ' (none here)'), got, [[], ['x', 'y'], ['x'], ['x'] if same else None, None])


# M: C4 held, E4 struck over it and released, C4 released, then G4 alone (96 ticks a quarter note,
# 500000 us): legato moves to E4 and back, retrigger note-ons, and glides of 4 steps of 5 ms.
M_TRACK = [(0, [0x90, 60, 100]), (48, [0x90, 64, 90]), (96, [0x80, 64, 0]), (144, [0x80, 60, 0]),
           (192, [0x90, 67, 80]), (240, [0x80, 67, 0]), (288, EOT)]
M_LEGATO = ['0 ch1 0 on 261626 25800', '250000000 ch1 0 pitch 329628', '500000000 ch1 0 pitch 261626',
            '750000000 ch1 0 off', '1000000000 ch1 0 on 391995 20640', '1250000000 ch1 0 off']
M_RETRIGGER = ['0 ch1 0 on 261626 25800', '250000000 ch1 0 on 329628 23220', '500000000 ch1 0 on 261626 25800',
               '750000000 ch1 0 off', '1000000000 ch1 0 on 391995 20640', '1250000000 ch1 0 off']
M_GLIDE = ['0 ch1 0 on 261626 25800', '255000000 ch1 0 pitch 277183', '260000000 ch1 0 pitch 293665',
           '265000000 ch1 0 pitch 311127', '270000000 ch1 0 pitch 329628', '505000000 ch1 0 pitch 311127',
           '510000000 ch1 0 pitch 293665', '515000000 ch1 0 pitch 277183', '520000000 ch1 0 pitch 261626',
           '750000000 ch1 0 off', '1000000000 ch1 0 on 391995 20640', '1250000000 ch1 0 off']
# K: a kit on channel 10: key 36 at 55 Hz, keys 42 and 46 at their 12-TET pitches in one choke
# group, and key 50 with no entry.
K_TRACK = [(0, [0x99, 36, 100]), (0, [0x99, 42, 100]), (48, [0x99, 46, 100]), (60, [0x89, 42, 0]),
           (72, [0x99, 42, 100]), (80, [0x89, 46, 0]), (90, [0x99, 50, 100]), (96, [0x89, 36, 0]),
           (100, [0x89, 50, 0]), (120, [0x89, 42, 0]), (192, EOT)]
K_LINES = ['0 ch10 0 on 55000 25800', '0 ch10 1 on 92499 25800', '250000000 ch10 1 off',
           '250000000 ch10 3 on 116541 25800', '375000000 ch10 3 off', '375000000 ch10 2 on 92499 25800',
           '500000000 ch10 0 off', '625000000 ch10 2 off']


def selftest_modes(ck, tmp, have):
    """One check or more for each of Mono, Unison, Layers and splits, Kits and chokes, Random,
    Scala and RPN 0, with the lines worked out by hand."""
    data = smf_bytes(0, 96, [M_TRACK])
    for name, argv, want in (('legato', ['--mono', '1=legato'], M_LEGATO),
                             ('retrigger', ['--mono', '1=retrigger'], M_RETRIGGER),
                             ('legato, low note priority', ['--mono', '1=legato:low'], [M_LEGATO[0]] + M_LEGATO[3:]),
                             ('legato, glide 20 ms', ['--mono', '1=legato', '--glide', '1=20'], M_GLIDE)):
        conv = Conversion(data, cfg_of('--map', '1=epiano', *argv))
        p = conv.piece('selftest-m', os.path.join(tmp, 'm'), 'm.mid')
        ck.lines('Mono: %s' % name, stream_lines(p, 'ch1'), want)
    ck.eq('Mono: the counts of the glide', [conv.st[0][k] for k in ('mono_ons', 'retriggers', 'legato_moves',
                                                                   'glide_events')] + [conv.voices(0)], [2, 1, 2, 8, 1])
    def bent_glide(track, division=96):
        conv = Conversion(smf_bytes(0, division, [track]),
                          cfg_of('--map', '1=epiano', '--mono', '1=legato', '--glide', '1=20'))
        return stream_lines(conv.piece('selftest-bend', os.path.join(tmp, 'bend'), 'bend.mid'), 'ch1')
    centered = sorted(M_TRACK + [(50, [0xE0] + data14(8192))], key=lambda e: e[0])
    ck.lines('Mono: centered bend during a glide preserves every scheduled step', bent_glide(centered), M_GLIDE)
    nonzero = [(0, [0xE0] + data14(12288))] + M_TRACK
    repeated = sorted(nonzero + [(50, [0xE0] + data14(12288))], key=lambda e: e[0])
    ck.lines('Mono: repeated nonzero bend preserves the running glide', bent_glide(repeated), bent_glide(nonzero))
    fine = [(t * 100, e) for t, e in M_TRACK]
    kept = sorted(fine + [(5001, [0xE0] + data14(12288))], key=lambda e: e[0])
    coalesced = sorted(kept + [(5000, [0xE0] + data14(12288))], key=lambda e: e[0])
    ck.lines('Mono: equal coalesced bends still apply their changed target',
             bent_glide(coalesced, 9600), bent_glide(kept, 9600))
    if have:
        print('     M: %s' % render(p, os.path.join(tmp, 'renders'), False)[0])
    conv = Conversion(data, cfg_of('--map', '1=epiano', '--mono', '1=retrigger', '--glide', '1=12'))
    got = stream_lines(conv.piece('selftest-m2', os.path.join(tmp, 'm2'), 'm.mid'), 'ch1')
    ck.lines('Mono: retrigger, glide 12 ms: 3 steps 4 ms apart, from the sounding pitch', got[1:5],
             ['250000000 ch1 0 on 261626 23220', '254000000 ch1 0 pitch 282572', '258000000 ch1 0 pitch 305194',
              '262000000 ch1 0 pitch 329628'])
    one = smf_bytes(0, 96, [[(0, [0x90, 69, 127]), (96, [0x80, 69, 0]), (192, EOT)]])
    conv = Conversion(one, cfg_of('--map', '1=epiano', '--unison', '1=3:20', '--voices', '3'))
    ck.lines('Unison: A4 on 3 voices at -10, 0 and +10 cents', stream_lines(
        conv.piece('selftest-u', os.path.join(tmp, 'u'), 'u.mid'), 'ch1'),
        ['0 ch1 0 on 437466 32767', '0 ch1 1 on 440000 32767', '0 ch1 2 on 442549 32767',
         '500000000 ch1 0 off', '500000000 ch1 1 off', '500000000 ch1 2 off'])
    two = smf_bytes(0, 96, [[(0, [0x90, 48, 100]), (0, [0x90, 72, 100]), (96, [0x80, 48, 0]), (96, [0x80, 72, 0]),
                             (192, EOT)]])
    conv = Conversion(two, cfg_of('--map', '1=round-bass@0-59,1=epiano:keys@60-127,1=flute@70-80'))
    p = conv.piece('selftest-z', os.path.join(tmp, 'z'), 'z.mid')
    ck.eq('Layers and splits: C3 to the bass, C5 to the keys and the flute layer, on streams ch1 and keys',
          [stream_lines(p, 'ch1'), stream_lines(p, 'keys'), list(p.patches), conv.st[0]['layered']],
          [['0 ch1 0 on 130813 25800', '0 ch1 2 on 523251 25800', '500000000 ch1 0 off', '500000000 ch1 2 off'],
           ['0 keys 1 on 523251 25800', '500000000 keys 1 off'], ['ch1z1', 'ch1z2', 'ch1z3'], 1])
    conv = Conversion(smf_bytes(0, 96, [K_TRACK]), cfg_of('--kit', '10=36:round-bass:55,42:epiano,46:epiano',
                                                          '--choke', '10=42+46'))
    p = conv.piece('selftest-k', os.path.join(tmp, 'k'), 'k.mid')
    ck.lines('Kits and chokes: each key its own patch, a choke off before the on', stream_lines(p, 'ch10'), K_LINES)
    ck.eq('Kits and chokes: the counts', [conv.st[9][k] for k in ('notes', 'kit_skipped', 'choked')] +
          [list(p.patches)], [4, 1, 2, ['ch10k36', 'ch10k42', 'ch10k46']])
    skipped_choke = Conversion(smf_bytes(0, 96, [K_TRACK]), cfg_of(
        '--kit', '10=36:round-bass:55,42:epiano,46:epiano', '--choke', '10=42+46+50'))
    ck.lines('Kits and chokes: a key without a kit entry cannot choke a sounding member',
             stream_lines(skipped_choke.piece('selftest-choke', os.path.join(tmp, 'choke'), 'choke.mid'), 'ch10'),
             K_LINES)
    ck.eq('Kits and chokes: skipped key adds no choke', skipped_choke.st[9]['choked'], 2)
    conv = Conversion(two, cfg_of('--map', '1=epiano', '--random', '1=7:50:10'))
    x1 = xorshift32(7)
    x2 = xorshift32(x1)
    ck.eq('Random: xorshift32 from seed 7, two draws a note', [x1, x2, x1 % 10001 - 5000, x2 % 21 - 10],
          [1892583, 470389255, -2606, -3])
    ck.lines('Random: C3 and C5 detuned and their velocities moved', stream_lines(
        conv.piece('selftest-r', os.path.join(tmp, 'r'), 'r.mid'), 'ch1'),
        ['0 ch1 0 on 128858 25026', '0 ch1 1 on 530329 26316', '500000000 ch1 0 off', '500000000 ch1 1 off'])
    expected_random = [(n.cents, n.vel) for n in conv.notes[0] if n.key == 72]
    zone = cfg_of('--map', '1=epiano@60-127', '--random', '1=7:50:10')
    tuning = cfg_of('--map', '1=epiano', '--random', '1=7:50:10')
    tuning.tuning = [compose.mhz_m(k) if k != 48 else None for k in range(128)]
    kit = cfg_of('--kit', '1=72:epiano', '--random', '1=7:50:10')
    for label, cfg in (('range', zone), ('.kbm', tuning), ('kit', kit)):
        skipped = Conversion(two, cfg)
        ck.eq('Random: a %s skip preserves every later strike\'s draws' % label,
              [(n.cents, n.vel) for n in skipped.notes[0]], expected_random)
    scl = os.path.join(tmp, '12.scl')
    with open(scl, 'w', encoding='utf-8', newline='\n') as f:
        f.write('! 12.scl\n12-EDO, in cents\n 12\n!\n' + ''.join('%d.0\n' % (100 * k) for k in range(1, 13)))
    ck.eq('Scala: 12-EDO in cents gives mhz_m for every key', cfg_of('--map', '1=epiano', '--scala', scl).tuning,
          [compose.mhz_m(k) for k in range(128)])
    just, kbm = os.path.join(tmp, 'just.scl'), os.path.join(tmp, 'a.kbm')
    with open(just, 'w', encoding='utf-8', newline='\n') as f:
        f.write('just\n3\n5/4\n3/2 a fifth\n2\n')
    with open(kbm, 'w', encoding='utf-8', newline='\n') as f:
        f.write('! a.kbm\n4\n60\n72\n60\n63\n440.0\n3\n0\nx\n1\n2\n')
    t = cfg_of('--map', '1=epiano', '--scala', just + ':' + kbm).tuning
    ck.eq('Scala: ratios exact, a .kbm with x, a range and a reference',
          [t[59], t[60], t[61], t[62], t[63], t[64], t[69], t[72], t[73]],
          [None, 293333, None, 366667, 440000, 586667, None, 2346667, None])
    rpn = smf_bytes(0, 96, [[(0, [0xB0, 101, 0]), (0, [0xB0, 100, 0]), (0, [0xB0, 6, 12]), (0, [0xB0, 38, 50]),
                             (0, [0xE0] + data14(16383)), (0, [0x90, 60, 100]), (96, [0x80, 60, 0]), (192, EOT)]])
    got = [stream_lines(Conversion(rpn, cfg_of('--map', '1=epiano', *argv)).piece(
        'selftest-p', os.path.join(tmp, 'p'), 'p.mid'), 'ch1')[0] for argv in ([], ['--rpn'])]
    ck.eq('RPN 0: 12 semitones 50 cents set the range only with --rpn', got,
          ['0 ch1 0 on 293661 25800', '0 ch1 0 on 538536 25800'])
    for cents in (99, 100, 127):
        rpn_cents = smf_bytes(0, 96, [[(0, [0xB0, 101, 0]), (0, [0xB0, 100, 0]),
                                     (0, [0xB0, 6, 2]), (0, [0xB0, 38, cents]),
                                     (0, [0x90, 69, 100]), (96, [0x80, 69, 0]), (192, EOT)]])
        limited = Conversion(rpn_cents, cfg_of('--map', '1=epiano', '--rpn'))
        ck.eq('RPN 0: CC38 %d clamps to 99 cents' % cents, limited.ranges[0][-1][2], Fraction(299, 100))


def selftest_mode_faults(ck, tmp):
    bad = os.path.join(tmp, 'bad.scl')
    with open(bad, 'w', encoding='utf-8', newline='\n') as f:
        f.write('bad\n2\n100.0\nnot a pitch\n')
    for argv in (['--map', '1=epiano', '--mono', '1=poly'], ['--map', '1=epiano', '--glide', '1=20'],
                 ['--map', '1=epiano', '--unison', '1=9:10'], ['--map', '1=epiano@60-200'],
                 ['--kit', '10=200:epiano'], ['--map', '1=epiano', '--choke', '1=42'],
                 ['--map', '1=epiano', '--random', '1=0:10'],
                 ['--map', '1=epiano', '--scala', bad], ['--map', '1=epiano', '--tuning', bad, '--scala', bad]):
        try:
            cfg_of(*argv)
            ck.ok('refused: %s' % ' '.join(argv).replace(tmp + os.sep, ''), False, 'accepted')
        except ValueError as e:
            ck.ok('refused: %s' % ' '.join(argv).replace(tmp + os.sep, ''),
                  'line 4' in str(e) if argv[-1] == bad and '--tuning' not in argv else True, str(e))
    scale, keyboard = os.path.join(tmp, 'line.scl'), os.path.join(tmp, 'line.kbm')
    with open(scale, 'w', encoding='utf-8', newline='\n') as f:
        f.write('octave\n1\n2\n')
    for field, value, line in ((1, '128', 4), (2, '128', 5), (3, '128', 6), (4, '128', 7),
                               (2, '59', 5)):
        header = ['0', '60', '127', '60', '69', '440.0', '1']
        header[field] = value
        with open(keyboard, 'w', encoding='utf-8', newline='\n') as f:
            f.write('! physical line numbers\n\n' + '\n'.join(header) + '\n')
        try:
            cfg_of('--map', '1=epiano', '--scala', scale + ':' + keyboard)
            ck.ok('Scala: invalid header field %d reports physical line %d' % (field, line), False, 'accepted')
        except ValueError as e:
            ck.ok('Scala: invalid header field %d reports physical line %d' % (field, line),
                  'line.kbm: line %d:' % line in str(e), str(e))


# Controller translation checks without MPE. Format 0,
# 96 ticks a quarter note at 96000 us, so a tick is 1 ms. Channel 1 (--ctl 1=default, recipe t37-all,
# --voices 2, --random 1=9:0, which draws but moves nothing): CC 1, 2 and 11 before the first note,
# a CC 1 ramp whose first two moves merge, channel pressure, poly pressure on two held keys, a
# note-off velocity, a pedal-held release, two note-ons that take released voices (their poly
# pressure and release velocity latches go back to the channel's values), a steal, a velocity-0
# note-on and a stolen note's key-up. Channel 2 (recipe t37-some, which reads ctl0, ctl2 and ctl6):
# a 14-bit CC 1/33 pair with an MSB that clears the LSB, an NRPN with data entry, data entry to an
# NRPN not mapped (counted), and pressure and poly pressure on inputs its patch does not read.
C_RECIPES = """recipe t37-all
mod 0 ctl0 index1 1000 0
mod 1 ctl1 level0 -60 0
mod 2 ctl2 pitch0 100 0
mod 3 ctl3 level0 -30 0
mod 4 ctl4 index1 0 0
mod 5 ctl5 index1 500 0
mod 6 ctl6 pitch1 50 0
carrier level=10 r=10
modulator index=1 r=10
note regression self-test: reads ctl0 to ctl3, ctl5 and ctl6; the ctl4 slot has amount 0
recipe t37-some
mod 0 ctl0 index1 1000 0
mod 1 ctl1 level0 0 0
mod 2 ctl2 pitch0 100 0
mod 3 ctl6 pitch1 50 0
carrier level=10 r=10
modulator index=1 r=10
note regression self-test: reads ctl0, ctl2 and ctl6; the ctl1 slot has amount 0
"""
C_EVENTS = [
    (0, tempo(96000)), (0, [0xB0, 1, 64]), (0, [0xB0, 2, 50]), (0, [0xB0, 11, 100]), (5, [0x91, 48, 100]),
    (10, [0x90, 60, 100]), (15, [0xB1, 1, 10]), (16, [0xB1, 33, 100]), (17, [0xB1, 1, 11]),
    (20, [0xB0, 1, 65]), (20, [0xB0, 1, 66]), (21, [0xB0, 1, 67]), (25, [0xB1, 99, 1]), (25, [0xB1, 98, 2]),
    (25, [0xB1, 6, 3]), (26, [0xB1, 38, 5]), (27, [0xB1, 98, 3]), (27, [0xB1, 6, 9]), (30, [0xD0, 40]),
    (35, [0xD1, 50]), (40, [0x90, 64, 90]), (45, [0xA1, 48, 60]), (50, [0xA0, 60, 30]), (50, [0xA0, 64, 20]),
    (55, [0x81, 48, 10]), (60, [0x80, 60, 100]), (70, [0xB0, 64, 127]), (80, [0x80, 64, 30]),
    (90, [0xB0, 64, 0]), (100, [0x90, 67, 100]), (101, [0x90, 69, 100]), (102, [0x90, 71, 100]),
    (120, [0x90, 69, 0]), (130, [0x80, 71, 127]), (150, [0x80, 67, 80]), (200, EOT)]
C_ARGS = ['--map', '1=t37-all,2=t37-some', '--voices', '2', '--random', '1=9:0', '--ctl', '1=default',
          '--ctl', '2=cc1+:0,nrpn1.2:2,pressure:1,polyat:3,rand:12345:6']
# Q15 values: CC 64 16513, 50 12901 (floor 12900), 100 25802 (floor 25801), 66 17029, 67 17287,
# pressure 40 10321 (floor 10320), poly 30 7740 and 20 5160, release 100 25802, 30 7740, 64 (the
# velocity-0 note-on) 16513 and 127 32768; 14-bit 1280 2560, 1380 2760, 1408 2816, 384 768, 389 778.
# rand: xorshift32 from 2654435769 (channel 1's default seed) gives (x >> 16) - 32768 = -12020,
# 24622, -1095, -32381, 26163, one draw a voice note-on; from 12345, 18149.
C_CH1 = ['10000000 ch1 0 ctl 0 16513', '10000000 ch1 0 ctl 2 12901', '10000000 ch1 0 ctl 3 25802',
         '10000000 ch1 0 ctl 6 -12020', '10000000 ch1 0 on 261626 25800', '20000000 ch1 0 ctl 0 17029',
         '21000000 ch1 0 ctl 0 17287', '30000000 ch1 0 ctl 1 10321', '40000000 ch1 1 ctl 0 17287',
         '40000000 ch1 1 ctl 1 10321', '40000000 ch1 1 ctl 2 12901', '40000000 ch1 1 ctl 3 25802',
         '40000000 ch1 1 ctl 6 24622', '40000000 ch1 1 on 329628 23220', '50000000 ch1 0 ctl 1 7740',
         '50000000 ch1 1 ctl 1 5160', '60000000 ch1 0 ctl 5 25802', '60000000 ch1 0 off',
         '90000000 ch1 1 ctl 5 7740', '90000000 ch1 1 off', '100000000 ch1 0 ctl 1 10321',
         '100000000 ch1 0 ctl 5 0', '100000000 ch1 0 ctl 6 -1095', '100000000 ch1 0 on 391995 25800',
         '101000000 ch1 1 ctl 1 10321', '101000000 ch1 1 ctl 5 0', '101000000 ch1 1 ctl 6 -32381',
         '101000000 ch1 1 on 440000 25800', '102000000 ch1 0 off', '102000000 ch1 0 ctl 6 26163',
         '102000000 ch1 0 on 493883 25800', '120000000 ch1 1 ctl 5 16513', '120000000 ch1 1 off',
         '130000000 ch1 0 ctl 5 32768', '130000000 ch1 0 off']
C_CH2 = ['5000000 ch2 2 ctl 6 18149', '5000000 ch2 2 on 130813 25800', '15000000 ch2 2 ctl 0 2560',
         '16000000 ch2 2 ctl 0 2760', '17000000 ch2 2 ctl 0 2816', '25000000 ch2 2 ctl 2 768',
         '26000000 ch2 2 ctl 2 778', '55000000 ch2 2 off']


def written_lines(path):
    """The event lines of a written timeline file, comments dropped and blanks squeezed."""
    with open(path, encoding='utf-8') as f:
        return [' '.join(w) for w in (line.split('#', 1)[0].split() for line in f) if w and w[0].isdigit()]


def selftest_ctl(ck, tmp, have=False):
    """--ctl: check 2 (the C fixture's events, written one by one, against the lists above), its
    counts, the refusals, and what --ctl leaves alone."""
    rpath = os.path.join(tmp, 't37-recipes.txt')
    with open(rpath, 'w', encoding='utf-8', newline='\n') as f:
        f.write(C_RECIPES)
    added = compose.load_recipes(rpath)
    compose.RECIPES.update(added)
    try:
        data = smf_bytes(0, 96, [C_EVENTS])
        conv = Conversion(data, cfg_of(*C_ARGS))
        p = conv.piece('selftest-c', os.path.join(tmp, 'c'), 'c.mid')
        p.write()
        for s, want in (('ch1', C_CH1), ('ch2', C_CH2)):
            ck.lines('ctl check 2: the events of %s as written' % s,
                     written_lines(os.path.join(tmp, 'c', s + '.tl')), want)
        keys = ('ctl_messages', 'ctl_kept', 'ctl_events', 'aftertouch', 'pressure', 'steals', 'pedal_held')
        ck.eq('ctl check 2: the counts of ch1 and ch2', [[conv.st[c][k] for k in keys] + [dict(conv.ccs[c])]
                                                          for c in (0, 1)],
              [[9, 8, 25, 0, 0, 1, 1, {}], [7, 7, 6, 0, 0, 0, 0, {6: 1}]])
        text = '\n'.join(conv.summary('selftest-c', 'c.mid'))
        ck.ok('Controllers: the report gives the map and the counts',
              '--ctl cc1:0 pressure:1 polyat:1 cc2:2 cc11:3 relvel:5 rand:2654435769:6: 9 messages (8 kept),'
              ' 25 ctl events' in text and 'ignored: CC 6 x1' in text, text)
        if have:
            print('     C: %s' % render(p, os.path.join(tmp, 'renders'), False)[0])
        plain = Conversion(data, cfg_of(*C_ARGS[:6]))
        ck.eq('Controllers: without --ctl nothing is written and the messages are counted',
              [sum(e[1] == 4 for e in plain.out[c]) for c in (0, 1)] +
              [[plain.st[c][k] for k in ('aftertouch', 'pressure')] + [dict(plain.ccs[c])] for c in (0, 1)],
              [0, 0, [2, 1, {1: 4, 2: 1, 11: 1}], [1, 1, {1: 2, 6: 2, 33: 1, 38: 1, 98: 2, 99: 1}]])
        ck.lines('Controllers: --ctl 1=default leaves a recipe without slots alone (fixture A)', stream_lines(
            Conversion(smf_bytes(0, 480, [A_TRACK]), cfg_of('--map', '1=epiano', '--voices', '2', '--ctl', '1=default')
                       ).piece('selftest-ca', os.path.join(tmp, 'ca'), 'a.mid'), 'ch1'), A_LINES)
        wide = smf_bytes(0, 96, [[(0, tempo(96000)), (0, [0x90, 60, 100]), (50, [0x80, 60, 0]), (100, EOT)]])
        got = stream_lines(Conversion(wide, cfg_of('--map', '1=t37-all', '--unison', '1=2:10', '--ctl', '1=default')
                                      ).piece('selftest-cu', os.path.join(tmp, 'cu'), 'u.mid'), 'ch1')
        ck.eq('Controllers: one draw a voice note-on, so each unison copy has its own',
              [l for l in got if ' ctl 6 ' in l], ['0 ch1 0 ctl 6 -12020', '0 ch1 1 ctl 6 24622'])
        drawn = [Conversion(data, cfg_of(*(C_ARGS[:4] + ['--random', '1=9:30:10'] + extra)))
                 for extra in ([], C_ARGS[6:8])]
        ck.eq('Controllers: rand leaves --random\'s draws where they were', *[
            [(n.cents, n.vel) for n in d.notes[0]] + [e[4] for e in d.out[0] if e[1] == 1] for d in drawn])
        restrike = smf_bytes(0, 96, [[(0, tempo(96000)), (0, [0xB0, 64, 127]), (0, [0x90, 60, 100]),
                                      (10, [0x80, 60, 40]), (20, [0x90, 60, 90]), (30, [0xB0, 64, 0]),
                                      (40, [0x80, 60, 70]), (100, EOT)]])
        got = stream_lines(Conversion(restrike, cfg_of('--map', '1=t37-all', '--ctl', '1=relvel:5')
                                      ).piece('selftest-cr', os.path.join(tmp, 'cr'), 'r.mid'), 'ch1')
        ck.eq('Controllers: a pedalled note struck again writes its key-up byte at the re-strike',
              [l for l in got if ' ctl ' in l], ['20000000 ch1 0 ctl 5 10321', '40000000 ch1 1 ctl 5 18061'])
        for argv in (['--ctl', '2=cc1:0'], ['--ctl', 'cc1:0'], ['--ctl', '1=cc1:8'], ['--ctl', '1=cc120:0'],
                     ['--ctl', '1=cc32+:0'], ['--ctl', '1=cc6:0'], ['--ctl', '1=cc64:0'], ['--ctl', '1=cc99:0'],
                     ['--ctl', '1=cc1:0,cc1:2'], ['--ctl', '1=cc1+:0,cc33:1'], ['--ctl', '1=default,cc2:4'],
                     ['--filter', '1=lp:1000:0.7', '--ctl', '1=cc74:0'], ['--ctl', '1=nrpn127.127:0'],
                     ['--ctl', '1=nrpn128.0:0'], ['--ctl', '1=rand:0:6'], ['--ctl', '1=rand:5:6,cc1:6'],
                     ['--ctl', '1=rand:5:6,rand:7:5'], ['--mono', '1=retrigger', '--ctl', '1=default'],
                     ['--ctl', '1=volume:0'], ['--ctl', '1=default x']):
            try:
                cfg_of('--map', '1=epiano', *argv)
                ck.ok('refused: --ctl %s' % ' '.join(argv), False, 'accepted')
            except ValueError as e:
                ck.ok('refused: %s' % ' '.join(argv), '--ctl' in str(e), str(e))
        ck.eq('Controllers: entries without CH= take the channel before them; cc74 without --filter',
              cfg_of('--map', '1=epiano,2=epiano', '--ctl', '2=cc74:0,pressure:1', '--ctl', '1=cc7:7').ctls,
              {1: {('cc', 74): 0, ('pressure',): 1}, 0: {('cc', 7): 7}})
    finally:
        for name in added:
            compose.RECIPES.pop(name, None)


# Z: --mpe (the docstring's MPE; regression step 6). Format 0, 96 ticks a quarter note at 96000 us,
# so a tick is 1 ms; --mpe lower:3, recipe t39-zone (reads ctl1 and ctl4, a 100 ms release), --voices
# 4. An MPE configuration message on channel 1 (counted, ignored); RPN 0 on member channel 2 sets the
# member range to 24 for the zone; a master bend of +1 before two overlapping notes, channel 2's C4
# (after its pressure 64) and channel 3's E4; the members bent in opposite directions under the master
# bend (+6 and -3), the master bend back to 0, channel 2's slide and channel 3's pressure; then channel 2
# reused while its C4 is in release: a bend back to the centre moves the C4, the new G4 takes the
# member, and the next member bend and the master's -1 show the C4 keeping its member bend of 0.
Z_RECIPES = """recipe t39-zone
mod 0 ctl1 level0 -60 0
mod 1 ctl4 index1 1000 0
carrier level=10 r=100
modulator index=1 r=100
note regression self-test: reads ctl1 and ctl4
"""
Z_EVENTS = [
    (0, tempo(96000)), (0, [0xB0, 101, 0]), (0, [0xB0, 100, 6]), (0, [0xB0, 6, 3]), (0, [0xB0, 101, 127]),
    (0, [0xB0, 100, 127]), (0, [0xB1, 101, 0]), (0, [0xB1, 100, 0]), (0, [0xB1, 6, 24]),
    (1, [0xE0] + data14(12288)), (10, [0xD1, 64]), (10, [0x91, 60, 100]), (20, [0x92, 64, 100]),
    (30, [0xE1] + data14(10240)), (30, [0xE2] + data14(7168)), (40, [0xE0] + data14(8192)),
    (45, [0xB1, 74, 100]), (50, [0xD2, 30]), (60, [0x81, 60, 0]), (70, [0x82, 64, 0]),
    (74, [0xE1] + data14(8192)), (75, [0x91, 67, 100]), (80, [0xE1] + data14(9216)),
    (85, [0xE0] + data14(4096)), (100, [0x81, 67, 0]), (300, EOT)]
Z_ARGS = ['--map', '1=t39-zone', '--voices', '4', '--mpe', 'lower:3']
# Pitches: 60 + 1 = C#4 277183; 64 + 1 = F4 349228; 60 + 1 + 6 = G4 391995; 64 + 1 - 3 = D4 293665;
# 60 + 6 = F#4 369994; 64 - 3 = C#4; C4 261626; G4; 67 + 3 = A#4 466164; 60 - 1 = B3 246942; 64 - 1 - 3
# = C4; 67 - 1 + 3 = A4 440000. Q15: pressure 64 16513, 30 7740; slide 100 25802.
Z_LINES = ['10000000 ch1 0 ctl 1 16513', '10000000 ch1 0 on 277183 25800', '20000000 ch1 1 on 349228 25800',
           '30000000 ch1 0 pitch 391995', '30000000 ch1 1 pitch 293665', '40000000 ch1 0 pitch 369994',
           '40000000 ch1 1 pitch 277183', '45000000 ch1 0 ctl 4 25802', '50000000 ch1 1 ctl 1 7740',
           '60000000 ch1 0 off', '70000000 ch1 1 off', '74000000 ch1 0 pitch 261626',
           '75000000 ch1 2 ctl 1 16513', '75000000 ch1 2 ctl 4 25802', '75000000 ch1 2 on 391995 25800',
           '80000000 ch1 2 pitch 466164', '85000000 ch1 0 pitch 246942', '85000000 ch1 1 pitch 261626',
           '85000000 ch1 2 pitch 440000', '100000000 ch1 2 off']


def plain_of(events, master=0):
    """The control of an --mpe fixture: its notes, pedal and master bends on one plain channel,
    every member bend, range, pressure, slide and configuration message left out."""
    out = []
    for tick, ev in events:
        if ev[0] >= 0xF0 or ev[0] < 0x80:
            out.append((tick, ev))
        elif ev[0] & 0x0F == master and ev[0] & 0xF0 in (0xB0,) and ev[1] in (6, 38, 98, 99, 100, 101):
            continue
        elif ev[0] & 0xF0 in (0x80, 0x90) or ev[0] & 0xF0 == 0xB0 and ev[1] == 64:
            out.append((tick, [ev[0] & 0xF0 | master] + list(ev[1:])))
        elif ev[0] & 0x0F == master and ev[0] & 0xF0 == 0xE0:
            out.append((tick, ev))
    return out


def selftest_mpe(ck, tmp, have=False):
    """--mpe: the Z fixture's events against the list above, its counts, the control (member bends
    at the centre equal one plain channel), --fs, the refusals and what --mpe leaves alone."""
    rpath = os.path.join(tmp, 't39-recipes.txt')
    with open(rpath, 'w', encoding='utf-8', newline='\n') as f:
        f.write(Z_RECIPES)
    added = compose.load_recipes(rpath)
    compose.RECIPES.update(added)
    try:
        data = smf_bytes(0, 96, [Z_EVENTS])
        conv = Conversion(data, cfg_of(*Z_ARGS))
        p = conv.piece('selftest-z', os.path.join(tmp, 'z'), 'z.mid')
        p.write()
        ck.lines('MPE check 6: two overlapping notes bent apart under a master bend, and a reused member',
                 written_lines(os.path.join(tmp, 'z', 'ch1.tl')), Z_LINES)
        keys = ('notes', 'mpe_notes', 'bends', 'bends_kept', 'mpe_slides', 'mpe_pressure', 'mpe_kept', 'mcm',
                'rpn_ranges', 'pitch_events', 'ctl_events')
        ck.eq('MPE: the counts of channels 1 to 3', [[conv.st[c][k] for k in keys] for c in (0, 1, 2)],
              [[3, 0, 3, 3, 0, 0, 0, 1, 0, 9, 5], [0, 2, 3, 3, 1, 1, 2, 0, 1, 0, 0], [0, 1, 1, 1, 0, 1, 1, 0, 0, 0, 0]])
        cents = Conversion(smf_bytes(0, 96, [[
            (0, tempo(96000)), (0, [0xB1, 101, 0]), (0, [0xB1, 100, 0]), (0, [0xB1, 6, 24]), (5, [0xB2, 101, 0]),
            (5, [0xB2, 100, 0]), (5, [0xB2, 38, 50]), (10, [0x92, 60, 100]), (20, [0x82, 60, 0]), (30, EOT)]]),
            cfg_of(*Z_ARGS))
        ck.eq('MPE: CC 38 on one member adds its cents to the semitones CC 6 gave on another',
              [e[4] for e in cents.zacts[0] if e[2] == 'range'], [Fraction(24), Fraction(49, 2)])
        text ='\n'.join(conv.summary('selftest-z', 'z.mid'))
        ck.ok('MPE: the report gives the zone, the members and the configuration message',
              'MPE lower zone, members ch2 to ch4: 3 member notes, 5 ctl events' in text and
              'ch2: MPE member of ch1: 2 notes, 3 bends (3 kept), 1 slide and 1 pressure message (2 kept);'
              ' 1 member range' in text and 'MPE configuration message 1' in text, text)
        if have:
            print('     Z: %s' % render(p, os.path.join(tmp, 'renders'), False)[0])
        zf = Conversion(data, cfg_of(*(Z_ARGS + ['--fs', '44100']))).piece('selftest-zf', os.path.join(tmp, 'zf'), 'z.mid')
        ck.eq('MPE: --fs 44100 gives the same events at 44100 Hz', (zf.fs, stream_lines(zf, 'ch1')),
              (44100, stream_lines(p, 'ch1')))
        flt = Conversion(smf_bytes(0, 96, [sorted(Z_EVENTS + [(46, [0xB0, 74, 90])], key=lambda e: e[0])]),
                         cfg_of(*(Z_ARGS + ['--filter', '1=lp:1000:0.7'])))
        ck.eq('MPE: CC 74 is never the filter event, a member\'s being slide and the master\'s a CC',
              [sum(e[1] == 3 for e in flt.out[0]), flt.st[0]['cc74'], flt.st[1]['mpe_slides'], flt.ccs[0][74]],
              [0, 0, 1, 1])
        centred = [(tk, ev[:1] + data14(8192) if ev[0] & 0xF0 == 0xE0 and ev[0] & 0x0F else ev)
                   for tk, ev in Z_EVENTS]
        a = Conversion(smf_bytes(0, 96, [centred]), cfg_of('--map', '1=epiano', '--voices', '4', '--mpe', 'lower:3'))
        b = Conversion(smf_bytes(0, 96, [plain_of(centred)]), cfg_of('--map', '1=epiano', '--voices', '4'))
        got = [stream_lines(x.piece('selftest-z' + n, os.path.join(tmp, 'z' + n), 'z.mid'), 'ch1')
               for x, n in ((a, 'a'), (b, 'b'))]
        ck.ok('MPE control: member bends at 8192 equal the same notes on one plain channel',
              got[0] == got[1] and len(got[0]) == 11, got)
        zn = Conversion(data, cfg_of(*Z_ARGS[:4]))
        ck.eq('MPE: without --mpe the members stay unmapped, their notes skipped (fixture Z)',
              [zn.channels(), zn.st[1]['skipped'], zn.st[2]['skipped'], zn.st[0]['ccs'], dict(zn.ccs[0])],
              [[], 2, 1, 0, {6: 1, 100: 2, 101: 2}])
        ck.eq('MPE: lower:7 and upper:7 meet without overlap; --ctl cc1:0 on a master is taken',
              cfg_of('--map', '1=epiano,16=epiano', '--mpe', 'lower:7,upper:7', '--ctl', '1=cc1:0').zone_of,
              {**{c: 0 for c in range(8)}, **{c: 15 for c in range(8, 16)}})
        for argv in (['--mpe', 'lower:0'], ['--mpe', 'lower:16'], ['--mpe', 'middle:3'], ['--mpe', 'lower:3,lower:4'],
                     ['--mpe', 'lower:8,upper:7'], ['--mpe', 'lower:15', '--mpe', 'upper:1'], ['--mpe', 'upper:3'],
                     ['--map', '3=epiano', '--mpe', 'lower:3'], ['--map', '1=epiano@60-127', '--mpe', 'lower:3'],
                     ['--mono', '1=retrigger', '--mpe', 'lower:3'], ['--unison', '1=2:10', '--mpe', 'lower:3'],
                     ['--random', '1=5:10', '--mpe', 'lower:3'], ['--choke', '1=36+38', '--mpe', 'lower:3'],
                     ['--ctl', '1=default', '--mpe', 'lower:3'], ['--ctl', '1=cc1:4', '--mpe', 'lower:3'],
                     ['--mpe', 'lower:3 x'], ['--fs', '32000']):
            try:
                cfg_of('--map', '1=epiano', *argv)
                ck.ok('refused: %s' % ' '.join(argv), False, 'accepted')
            except ValueError as e:
                ck.ok('refused: %s' % ' '.join(argv), argv[-2] in str(e) or '--mpe' in str(e), str(e))
    finally:
        for name in added:
            compose.RECIPES.pop(name, None)


def selftest_scale(ck, tmp, have=False):
    """Scale locks alter pitch, never the key that identifies a note or its messages."""
    def convert(track, *args):
        return Conversion(smf_bytes(0, 96, [[(0, tempo(96000))] + track]),
                          cfg_of('--map', '1=epiano', *args))
    def lines(conv, name='scale'):
        return stream_lines(conv.piece('selftest-' + name, os.path.join(tmp, name), name + '.mid'), 'ch1')
    def refuse(label, run):
        try:
            run()
            ck.ok('Scale refused: ' + label, False, 'accepted')
        except ValueError as e:
            ck.ok('Scale refused: ' + label, '--scale' in str(e), str(e))

    cfg = cfg_of('--map', '1=epiano,2=bell,3=flute', '--scale', '1=major:2,2=2741:2',
                 '--scale', '3=minor')
    ck.eq('Scale: presets, decimal masks, root default, comma and repeated options',
          cfg.scales, {0: (2741, 2), 1: (2741, 2), 2: (1453, 0)})
    track = []
    for i, key in enumerate(range(60, 73)):
        track += [(i * 20, [0x90, key, 100]), (i * 20 + 10, [0x80, key, 0])]
    track += [(300, EOT)]
    conv = convert(track, '--scale', '1=major:2')
    want = [59, 61, 62, 62, 64, 64, 66, 67, 67, 69, 69, 71, 71]
    ck.eq('Scale: D major keys 60 to 72 snap down before pitch conversion',
          [int(x.split()[4]) for x in lines(conv) if x.split()[3] == 'on'],
          [compose.mhz_m(k) for k in want])
    ck.eq('Scale: file keys remain distinct from locked pitches',
          [(n.key, n.pitch_key()) for n in conv.notes[0]], list(zip(range(60, 73), want)))
    ck.lines('Scale: mask zero preserves every unscaled event',
             lines(convert(track, '--scale', '1=0:11')), lines(convert(track)))

    pair = [(0, [0x90, 62, 100]), (10, [0x90, 63, 100]), (20, [0x80, 62, 0]),
            (30, [0x80, 63, 0]), (100, EOT)]
    conv = convert(pair, '--scale', '1=major:2')
    ck.lines('Scale: two snapped keys retain two voices and their own offs', lines(conv),
             ['0 ch1 0 on 293665 25800', '10000000 ch1 1 on 293665 25800',
              '20000000 ch1 0 off', '30000000 ch1 1 off'])
    ck.lines('Scale: ranges select the file key, not its locked pitch',
             lines(Conversion(smf_bytes(0, 96, [[(0, tempo(96000))] + pair]),
                              cfg_of('--map', '1=epiano@63-63', '--scale', '1=major:2'))),
             ['10000000 ch1 0 on 293665 25800', '30000000 ch1 0 off'])
    pedal = sorted(pair[:-1] + [(0, [0xB0, 64, 127]), (40, [0xB0, 64, 0])],
                   key=lambda x: x[0]) + [(100, EOT)]
    ck.lines('Scale: pedal holds both file keys until its release',
             lines(convert(pedal, '--scale', '1=major:2')),
             ['0 ch1 0 on 293665 25800', '10000000 ch1 1 on 293665 25800',
              '40000000 ch1 0 off', '40000000 ch1 1 off'])
    ck.lines('Scale: a choke still finds the file key after equal pitches',
             lines(convert(pair, '--scale', '1=major:2', '--choke', '1=62+63')),
             ['0 ch1 0 on 293665 25800', '10000000 ch1 0 off',
              '10000000 ch1 1 on 293665 25800', '30000000 ch1 1 off'])
    ck.lines('Scale: stealing retains the distinct file-key note-offs',
             lines(convert(pair, '--scale', '1=major:2', '--voices', '1')),
             ['0 ch1 0 on 293665 25800', '10000000 ch1 0 off',
              '10000000 ch1 0 on 293665 25800', '30000000 ch1 0 off'])
    for args in (('--unison', '1=2:20'), ('--map', '1=epiano@60-72')):
        # Layers use a second range beside the existing full-channel map.
        repeated = convert(pair, '--scale', '1=major:2', *args)
        ck.eq('Scale: copies retain their locked key (%s)' % args[0],
              [(n.key, n.pitch_key()) for n in repeated.notes[0]],
              [(62, 62), (62, 62), (63, 62), (63, 62)])
    plain = convert(pair, '--random', '1=7:50:10')
    locked = convert(pair, '--random', '1=7:50:10', '--scale', '1=major:2')
    ck.eq('Scale: random detune and velocity draws keep their order',
          [(n.cents, n.vel) for n in locked.notes[0]],
          [(n.cents, n.vel) for n in plain.notes[0]])

    rpath = os.path.join(tmp, 't29-scale-recipes.txt')
    with open(rpath, 'w', encoding='utf-8', newline='\n') as f:
        f.write(C_RECIPES)
    added = compose.load_recipes(rpath)
    compose.RECIPES.update(added)
    try:
        poly = sorted(pair[:-1] + [(15, [0xA0, 63, 127])], key=lambda x: x[0]) + [(100, EOT)]
        conv = Conversion(smf_bytes(0, 96, [[(0, tempo(96000))] + poly]),
                          cfg_of('--map', '1=t37-all', '--scale', '1=major:2', '--ctl', '1=polyat:1'))
        ck.eq('Scale: poly pressure reaches only its original file key',
              [x for x in lines(conv) if x.startswith('15000000 ')],
              ['15000000 ch1 1 ctl 1 32768'])
    finally:
        for name in added:
            del compose.RECIPES[name]

    tuning = os.path.join(tmp, 'scale-tuning.txt')
    with open(tuning, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(str(100000 + 1000 * k) for k in range(128)))
    bent = [(0, [0xE0] + data14(12288)), (0, [0x90, 63, 100]), (20, [0x80, 63, 0]), (100, EOT)]
    ck.eq('Scale: lock precedes a tuning table and the bend',
          [x.split()[4] for x in lines(convert(bent, '--scale', '1=major:2', '--tuning', tuning))
           if x.split()[3] == 'on'], [str(pitch_mhz(162000, 1, 12))])

    scl, kbm = os.path.join(tmp, 'scale.scl'), os.path.join(tmp, 'scale.kbm')
    with open(scl, 'w', encoding='utf-8', newline='\n') as f:
        f.write('twelve ratios\n12\n16/15\n9/8\n6/5\n5/4\n4/3\n45/32\n3/2\n8/5\n5/3\n9/5\n15/8\n2\n')
    def write_kbm(size):
        with open(kbm, 'w', encoding='utf-8', newline='\n') as f:
            f.write('%d\n0\n127\n60\n60\n100.0\n12\n0\n1\n7\nx\n4\n5\n6\n7\n8\n9\n10\n11\n' % size)
    write_kbm(12)
    single = [(0, [0x90, 63, 100]), (20, [0x80, 63, 0]), (100, EOT)]
    cfg = cfg_of('--map', '1=epiano', '--scale', '1=major:2', '--scala', scl + ':' + kbm)
    conv = Conversion(smf_bytes(0, 96, [[(0, tempo(96000))] + single]), cfg)
    ck.lines('Scale: lock before .kbm lookup maps an otherwise unmapped file key',
             lines(conv, 'scale-kbm') if conv.notes[0] else [],
             ['0 ch1 0 on 150000 25800', '20000000 ch1 0 off'])
    ck.eq('Scale: .kbm uses the locked key mapping, not its file key',
          [cfg.tuning[62], cfg.tuning[63], conv.st[0]['unmapped_keys']], [150000, None, 0])
    if have:
        p = conv.piece('selftest-scale', os.path.join(tmp, 'scale-render'), 'scale.mid')
        print('     SCALE: %s' % render(p, os.path.join(tmp, 'renders'), False)[0])
    for size in (0, 4, 13):
        write_kbm(size)
        refuse('.kbm map size %d' % size,
               lambda: cfg_of('--map', '1=epiano', '--scale', '1=major:2', '--scala', scl + ':' + kbm))
    for value in ('1=missing', '1=4096', '1=-1', '1=1.5', '1=major:12', '1=major:-1',
                  '0=major', '17=major', '2=major', '1=major,1=minor', '1=major extra'):
        refuse(value, lambda: cfg_of('--map', '1=epiano', '--scale', value))
    for args in (('--kit', '1=36:epiano'), ('--map', '1=epiano', '--mono', '1=legato'),
                 ('--map', '1=epiano', '--mpe', 'lower:3'),
                 ('--map', '16=epiano', '--mpe', 'upper:15')):
        refuse('incompatible channel ' + ' '.join(args), lambda: cfg_of(*args, '--scale', '1=major'))
    low = [(0, [0x90, 0, 100]), (20, [0x80, 0, 0]), (100, EOT)]
    refuse('key would lock below zero', lambda: convert(low, '--scale', '1=1:1'))


def selftest_arp_conditions(ck):
    """ conditions operate on logical expanded steps, preserving draws."""
    def data(track):return smf_bytes(0,32,[[(0,tempo(32000))]+track])
    def convert(track,*args):
        return Conversion(data(track),cfg_of('--map','1=epiano','--voices','16','--arp','1=up:1/32',*args))
    def gs(conv,c=0):return [n._arp_step for n in conv.notes[c]]
    def refuse(label,run,option):
        try:run()
        except ValueError as e:ck.ok('Arp conditions refuse '+label,option in str(e),str(e))
        else:ck.ok('Arp conditions refuse '+label,False,'accepted')
    held=[(0,[0x90,60,100]),(32,[0x80,60,0]),(32,EOT)]
    pattern='1=0:pre,1=1:not-pre,1=2:pre,1=4:not-pre,1=5:pre,1=6:not-pre,1=7:pre'
    c=convert(held,'--arp-cond',pattern)
    ck.eq('Arp conditions literal PRE chain with unconditional gap',gs(c),[1,2,3,6,7])
    ck.eq('Arp conditions preserve absolute grid timestamps',[n.on for n in c.notes[0]],
          [4000000,8000000,12000000,24000000,28000000])
    ck.eq('Arp conditions note counters follow filtered notes',(c.st[0]['notes'],c.st[0]['condition_skipped']),(5,3))
    ck.eq('Arp conditions unconditioned first step leaves PRE false',
          gs(convert(held,'--arp-cond','1=1:pre,1=2:not-pre')),[0,2,3,4,5,6,7])
    sparse=[(0,[0x90,60,100]),(4,[0x80,60,0]),(12,[0x90,64,100]),(32,[0x80,64,0]),(32,EOT)]
    ck.eq('Arp conditions empty conditional step clears PRE',
          gs(convert(sparse,'--arp-cond','1=0:not-pre,1=1:pre,1=3:pre,1=4:not-pre')),[0,4,5,6,7])
    split=[(0,[0x90,60,100]),(4,[0x80,60,0]),(4,[0x90,64,100]),(8,[0x80,64,0]),
           (8,[0x90,60,100]),(32,[0x80,60,0]),(32,EOT)]
    c=Conversion(data(split),cfg_of('--map','1=epiano@60-60','--arp','1=up:1/32',
                  '--arp-cond','1=0:not-pre,1=1:pre,1=2:pre,1=3:not-pre'))
    ck.eq('Arp conditions expansion-filtered conditional step clears PRE',gs(c),[0,3,4,5,6,7])
    neighbor=[(0,[0x90,60,100]),(0,[0x91,67,100]),(0,[0x92,72,100]),(4,[0x80,60,0]),
              (12,[0x90,64,100]),(20,[0x80,64,0]),(32,[0x81,67,0]),(32,[0x82,72,0]),(32,EOT)]
    nc=Conversion(data(neighbor),cfg_of('--map','1=epiano,2=bell,3=epiano','--voices','16',
        '--arp','1=up:1/32,2=chord:1/32,3=down:1/32','--arp-nei','2=1,3=2',
        '--arp-cond',','.join('%d=%d:nei'%(ch,g) for ch in (2,3) for g in range(8))))
    ck.eq('Arp conditions lower-channel neighbor chain uses actually played steps',
          [gs(nc,c) for c in range(3)],[[0,3,4]]*3)
    changed=Conversion(data(neighbor),cfg_of('--map','1=epiano,2=bell','--voices','16',
        '--arp','1=up:1/32,2=chord:1/32','--arp-nei','2=1','--arp-cond','1=0:pre',
        '--arp-cond',','.join('2=%d:nei'%g for g in range(8))))
    ck.eq('Arp conditions source rejection propagates to NEI',[gs(changed,c) for c in (0,1)],[[3,4],[3,4]])
    # Both selection and nonzero strike draws happen before the condition filter;
    # all same-step layers/unison copies either survive together or disappear.
    chord=[(0,[0x90,60,100]),(0,[0x90,64,90]),(32,[0x80,60,0]),(32,[0x80,64,0]),(32,EOT)]
    def random_conv(*opts):
        return Conversion(data(chord),cfg_of('--map','1=epiano,1=bell@0-127','--voices','32',
            '--arp','1=random:1/32','--arp-seed','1=7','--random','1=7:50:10','--unison','1=2:20',*opts))
    plain=random_conv();filtered=random_conv('--arp-cond',pattern)
    show=lambda ns:[(n._arp_step,n.on,n.key,n.vel,n.cents,n.patch) for n in ns]
    ck.eq('Arp conditions retain selection/strike draws and layer/unison groups',show(filtered.notes[0]),
          show([n for n in plain.notes[0] if n._arp_step in (1,2,3,6,7)]))
    ck.eq('Arp conditions four expanded copies per played step',len(filtered.notes[0]),20)
    ck.eq('Arp conditions strike counters survive layers/unison',
          (filtered.st[0]['notes'],filtered.st[0]['condition_skipped']),(5,3))
    ck.ok('Arp conditions summary names skipped strikes',
          '3 conditional strikes skipped' in filtered.said_modes(0),filtered.said_modes(0))
    # Played is established before voice stealing, not reconstructed from output.
    steal=Conversion(data(neighbor),cfg_of('--map','1=epiano,2=bell','--voices','1',
        '--arp','1=up:1/32:1:2,2=up:1/32','--arp-nei','2=1',
        '--arp-cond',','.join('2=%d:nei'%g for g in range(8))))
    ck.eq('Arp conditions NEI uses pre-steal played steps',gs(steal,1),[0,3,4])
    for value in ('1=-1:pre','1=0:first','1=0:PRE','1=x:pre','1=0:pre extra',
                  '1=0:pre,1=0:not-pre','2=0:pre','1=0:nei'):
        refuse(value,lambda value=value:convert(held,'--arp-cond',value),'--arp-cond')
    refuse('condition without arp',lambda:cfg_of('--map','1=epiano','--arp-cond','1=0:pre'),'--arp-cond')
    refuse('out of range index',lambda:convert(held,'--arp-cond','1=8:pre'),'--arp-cond')
    refuse('index in empty grid',lambda:convert([(0,EOT)],'--arp-cond','1=0:pre'),'--arp-cond')
    def neighbors(value,*extra):return cfg_of('--map','1=epiano,2=bell,3=epiano',
        '--arp','1=up:1/32,2=up:1/32','--arp-nei',value,*extra)
    for value in ('1=1','1=2','2=3','3=1','2=4','2=0','2=x','2=1 extra','2=1,2=1'):
        refuse('source '+value,lambda value=value:neighbors(value),'--arp-nei')
    refuse('different integer grids',lambda:Conversion(data(neighbor),cfg_of('--map','1=epiano,2=bell',
        '--arp','1=up:1/16,2=up:1/32','--arp-nei','2=1')),'--arp-nei')
    refuse('NEI source missing despite another neighbor',lambda:cfg_of('--map','1=epiano,2=bell,3=epiano',
        '--arp','1=up:1/32,2=up:1/32,3=up:1/32','--arp-nei','2=1','--arp-cond','3=0:nei'),'--arp-cond')


def selftest_groove(ck, tmp, have=False):
    """regression: literal held/latch clocks, source identity and chord spread controls."""
    def convert(track, *args, division=32, us=32000, recipe="epiano"):
        data = smf_bytes(0, division, [[(0, tempo(us))] + track])
        return Conversion(data, cfg_of('--map', '1=' + recipe, '--voices', '16', *args))
    def refuse(label, run, option):
        try:
            run()
            ck.ok('Groove refused: ' + label, False, 'accepted')
        except ValueError as e:
            ck.ok('Groove refused: ' + label, option in str(e), str(e))
    def notes(conv):
        return [(n.on, n.key, n.vel, n.origin_key) for n in conv.notes[0]]
    def ons(conv):
        return [(n.on, n.key) for n in conv.notes[0]]
    def lines(conv, name='groove'):
        return stream_lines(conv.piece('selftest-' + name, os.path.join(tmp, name), name + '.mid'), 'ch1')

    for denominator in (1, 2, 4, 8, 16, 32):
        for suffix, factor in (('', 1), ('d', Fraction(3, 2)), ('t', Fraction(2, 3))):
            cfg = cfg_of('--map', '1=epiano', '--arp', '1=up:1/%d%s' % (denominator, suffix))
            ck.eq('Groove RATE 1/%d%s' % (denominator, suffix), cfg.arps[0][:4],
                  ('up', Fraction(1, denominator) * factor, 1, Fraction(4, 5)))
    cfg = cfg_of('--map', '1=epiano,2=bell', '--arp', '1=up:1/8:2:3/2,2=down:1/16d',
                 '--arp-latch', '1,2', '--arp-seed', '1=0', '--arp-seed', '2=4294967295')
    ck.eq('Groove latch/seed repeat and comma spellings',
          (cfg.arp_latches, cfg.arp_seeds, cfg.arps[0][3]), ({0, 1}, {0: 0, 1: 4294967295}, Fraction(3, 2)))

    keys = [(0, [0x90, 60, 100]), (4, [0x90, 64, 90]), (8, [0x80, 60, 0]),
            (12, [0x80, 64, 0]), (16, [0x90, 67, 80]), (18, [0x80, 67, 0]),
            (19, [0x90, 62, 70]), (21, [0x80, 62, 0]), (28, EOT)]
    plain = convert(keys, '--arp', '1=chord:1/32:1:0.25')
    latched = convert(keys, '--arp', '1=chord:1/32:1:0.25', '--arp-latch', '1')
    ck.eq('Groove held keys: partial releases and empty rests', ons(plain),
          [(0, 60), (4000000, 60), (4000000, 64), (8000000, 64), (16000000, 67), (20000000, 62)])
    ck.eq('Groove latch: last physically held set, then replacement', ons(latched),
          [(0, 60), (4000000, 60), (4000000, 64), (8000000, 64), (12000000, 64),
           (16000000, 67), (20000000, 62), (24000000, 62)])
    ck.eq('Groove velocities and physical key IDs survive latch',
          [(n.key, n.vel, n.origin_key) for n in latched.notes[0]],
          [(60, 100, 60), (60, 100, 60), (64, 90, 64), (64, 90, 64),
           (64, 90, 64), (67, 80, 67), (62, 70, 62), (62, 70, 62)])
    together = [(0, [0x90, 60, 100]), (0, [0x90, 64, 90]), (4, [0x80, 60, 0]),
                (4, [0x80, 64, 0]), (12, EOT)]
    ck.eq('Groove simultaneous final releases retain the complete chord',
          ons(convert(together, '--arp', '1=chord:1/32', '--arp-latch', '1')),
          [(0, 60), (0, 64), (4000000, 60), (4000000, 64), (8000000, 60), (8000000, 64)])
    tied = [(0, [0x90, 60, 100]), (4, [0x90, 67, 90]), (4, [0x80, 60, 0]),
            (8, [0x80, 67, 0]), (8, EOT)]
    for latch in ((), ('--arp-latch', '1')):
        ck.eq('Groove equal-time transition order despite on-before-off file order ' + str(latch),
              ons(convert(tied, '--arp', '1=up:1/32', *latch)), [(0, 60), (4000000, 67)])
    duplicates = [(0, [0x90, 60, 100]), (0, [0x90, 60, 90]), (4, [0x80, 60, 0]),
                  (8, [0x80, 60, 0]), (12, EOT)]
    ck.eq('Groove duplicate pitches preserve paired press identity',
          [(n.on, n.vel) for n in convert(duplicates, '--arp', '1=chord:1/32', '--arp-latch', '1').notes[0]],
          [(0, 100), (0, 90), (4000000, 90), (8000000, 90)])
    random_keys = [(4, [0x90, k, 100]) for k in range(60, 64)] + [(12, EOT)]
    random_conv = convert(random_keys, '--arp', '1=random:1/32')
    ck.eq('Groove random empty step takes its draw: literal seed0 notes', ons(random_conv),
          [(4000000, 61), (8000000, 63)])
    randomized = convert(random_keys, '--arp', '1=random:1/32', '--random', '1=7:0:0')
    ck.eq('Groove arp random stream independent of strike randomization', ons(randomized), ons(random_conv))
    ordered = [(0, [0x90, 67, 100]), (0, [0x90, 60, 90]), (0, [0x90, 64, 80]), (24, EOT)]
    for mode, want in (('up', [60,64,67,60,64,67]), ('down', [67,64,60,67,64,60]),
                       ('updown', [60,64,67,64,60,64]), ('downup', [67,64,60,64,67,64]),
                       ('played', [67,60,64,67,60,64])):
        ck.eq('Groove mode ' + mode, [n.key for n in convert(ordered, '--arp', '1=%s:1/32' % mode).notes[0]], want)

    tempo_keys = [(0, [0x90, 60, 100]), (48, tempo(1000000)), (96, [0x80, 60, 0]), (96, EOT)]
    conv = convert(tempo_keys, '--arp', '1=up:1/8:1:1', division=96, us=500000)
    ck.eq('Groove changing tempo integrates exact tick endpoints',
          [(n.on, n.off) for n in conv.notes[0]], [(0, 250000000), (250000000, 750000000)])
    short = convert([(0,[0x90,60,100]),(1,[0x80,60,0]),(1,EOT)], '--arp', '1=up:1/32:1:0.125', division=8, us=8000)
    ck.eq('Groove positive sub-ms gate uses existing minimum duration',
          [(n.on,n.off) for n in short.notes[0]], [(0,1000000)])
    overlap = convert([(0,[0x90,60,100]),(12,EOT)], '--arp', '1=up:1/32:1:2')
    ck.eq('Groove gate above1 overlaps and the final off extends record length',
          [(n.on,n.off) for n in overlap.notes[0]], [(0,8000000),(4000000,12000000),(8000000,16000000)])
    refuse('noninteger PPQN step', lambda: convert(keys,'--arp','1=up:1/32',division=3), '--arp')
    smpte = smf_bytes(0, (256-25)*256+40, [[(0,[0x90,60,100]),(500,[0x80,60,0]),(501,EOT)]])
    refuse('SMPTE arp', lambda: Conversion(smpte,cfg_of('--map','1=epiano','--arp','1=up:1/8')), '--arp')
    ck.eq('Groove chord preserves supported SMPTE clock',
          [n.on for n in Conversion(smpte,cfg_of('--map','1=epiano','--chord','1=0+4+7:200')).notes[0]],
          [0,100000000,200000000])

    pedal = [(0,[0x90,60,100]),(1,[0xB0,64,127]),(2,[0x80,60,0]),(8,[0xB0,64,0]),(12,EOT)]
    ck.eq('Groove pedal extends held membership before selection',
          ons(convert(pedal,'--arp','1=up:1/32')), [(0,60),(4000000,60)])
    program = [(0,[0x90,60,100]),(1,[0xC0,1]),(4,[0x90,64,90]),(8,[0x80,60,0]),(12,[0x80,64,0]),(16,EOT)]
    conv = convert(program,'--arp','1=chord:1/32','--arp-latch','1','--program','2=bell')
    ck.eq('Groove held/latch retains each press program patch',
          [(n.on,n.key,n.patch) for n in conv.notes[0]],
          [(0,60,(0,None)),(4000000,60,(0,None)),(4000000,64,(0,2)),(8000000,64,(0,2)),(12000000,64,(0,2))])
    bent = [(0,[0x90,60,100]),(0,[0xE0]+data14(12288)),(4,[0x80,60,0]),(4,EOT)]
    conv = convert(bent,'--arp','1=up:1/32')
    ck.eq('Groove arp onset follows same-time original bend even after original key strike',
          [x.split()[4] for x in lines(conv) if x.split()[3]=='on'],[str(note_mhz(60,Fraction(1)))])
    layered = convert([(0,[0x90,60,100]),(8,EOT)],'--arp','1=up:1/32:2','--map','1=bell@72-72','--unison','1=2:0')
    ck.eq('Groove generated pitch selects ranges before unison; origin stays physical',
          [(n.key,n.origin_key) for n in layered.notes[0]],[(60,60),(60,60),(72,60),(72,60),(72,60),(72,60)])
    stolen = convert(ordered,'--arp','1=chord:1/32','--voices','1')
    ck.ok('Groove generated chord uses existing voice steals',stolen.st[0]['steals']>0,stolen.st[0])

    for size in range(1,9):
        for spread in (0,1,199,200):
            shape=tuple(range(size))
            conv=convert([(0,[0x90,60,100]),(500,[0x80,60,55]),(501,EOT)],
                         '--chord','1=%s:%d'%('+'.join(map(str,shape)),spread),division=1000,us=1000000)
            p=compose.Piece('strum-reference',120,4,['test'])
            p.patch('p',*compose.recipe('epiano'));p.stream('a','test')
            expected=p.chord('a',p.add_voices('p',8,'test'),60,0,500000000,100,shape,strum_ms=spread,release_ms=0)
            ck.eq('Groove chord matches Piece.chord n%d spread%d'%(size,spread),
                  [(n.on,n.off,n.key,n.up) for n in conv.notes[0]],[(a,b,int(key),55) for a,b,_,key in expected])
    seven=convert([(0,[0x90,60,100]),(500,[0x80,60,0]),(501,EOT)],'--chord','1=0+1+2+3+4+5+6:200',division=1000,us=1000000)
    ck.eq('Groove chord independent seven-note rounded spread', [n.on for n in seven.notes[0]],
          [0,33333333,66666667,100000000,133333333,166666667,200000000])
    refuse('strum reaches release at original tick/key',lambda:convert([(0,[0x90,60,100]),(200,[0x80,60,0]),(201,EOT)],
             '--chord','1=0+4:200',division=1000,us=1000000),'tick 0 key 60')
    high=[(0,[0x90,127,100]),(4,[0x80,127,0]),(4,EOT)]
    refuse('arp octave outside MIDI',lambda:convert(high,'--arp','1=up:1/32:2'),'--arp')
    refuse('chord outside MIDI',lambda:convert(high,'--chord','1=0+1'),'--chord')
    for option,value in [('--arp','1=bad:1/8'),('--arp','1=up:1/3'),('--arp','1=up:1/64'),
                         ('--arp','1=up:1/8:0'),('--arp','1=up:1/8:12'),('--arp','1=up:1/8:1:0'),
                         ('--arp','1=up:1/8:1:NaN'),('--arp','1=up:1/8:1:inf'),('--arp','1=up:1/8:1:1/0'),
                         ('--arp','1=up:1/8,1=down:1/4'),('--arp','2=up:1/8'),
                         ('--chord','1=4+7'),('--chord','1=0+4+4'),('--chord','1=0+7+4'),
                         ('--chord','1=0+1+2+3+4+5+6+7+8'),('--chord','1=0+4:201'),
                         ('--chord','1=0+4:0.0000001'),('--arp-latch','1'),('--arp-seed','1=1'),
                         ('--arp-seed','1=4294967296')]:
        refuse(option+' '+value,lambda option=option,value=value:cfg_of('--map','1=epiano',option,value),option)
    for option in ('--arp','--chord'):
        value='1=up:1/8' if option=='--arp' else '1=0+4'
        for extra in (('--scale','1=major'),('--mono','1=legato'),('--mpe','lower:3')):
            refuse(option+' plus '+extra[0],lambda extra=extra:cfg_of('--map','1=epiano',option,value,*extra),option)
        refuse(option+' plus kit',lambda:cfg_of('--kit','1=60:epiano',option,value),option)
    refuse('both transforms',lambda:cfg_of('--map','1=epiano','--arp','1=up:1/8','--chord','1=0+4'),'--arp')
    refuse('synthetic relvel',lambda:cfg_of('--map','1=epiano','--arp','1=up:1/8','--ctl','1=relvel:0'),'--arp')

    for denominator in (1, 2, 4, 8, 16, 32):
        for suffix, step in (('', Fraction(384, denominator)),
                             ('d', Fraction(576, denominator)),
                             ('t', Fraction(256, denominator))):
            conv = convert([(0, [0x90, 60, 100]), (768, EOT)],
                           '--arp', '1=up:1/%d%s:1:1' % (denominator, suffix),
                           division=96, us=96000)
            ck.eq('Groove RATE native tick grid 1/%d%s' % (denominator, suffix),
                  [n.on for n in conv.notes[0]], list(range(0, 768000000, int(step) * 1000000)))
    repeated = convert([(0, [0x90, 60, 100]), (1, [0xB0, 64, 127]),
                        (2, [0x80, 60, 0]), (4, [0x90, 60, 90]),
                        (6, [0x80, 60, 0]), (8, [0xB0, 64, 0]), (12, EOT)],
                       '--arp', '1=chord:1/32', '--arp-latch', '1')
    ck.eq('Groove restrike under pedal replaces the original press before selection',
          [(n.on, n.vel) for n in repeated.notes[0]], [(0, 100), (4000000, 90), (8000000, 90)])
    choke = convert([(0, [0x90, 60, 100]), (4, [0x90, 64, 90]), (12, EOT)],
                    '--arp', '1=chord:1/32:1:2', '--choke', '1=60+64')
    ck.eq('Groove source choke changes held set without choking generated gate',
          [(n.on, n.off, n.key) for n in choke.notes[0]],
          [(0, 8000000, 60), (4000000, 12000000, 64), (8000000, 16000000, 64)])
    short_chord = convert([(0, [0x90, 60, 100]), (0, [0x90, 64, 90]), (1, EOT)],
                          '--arp', '1=chord:1/32:1:0.125', division=8, us=8000)
    ck.eq('Groove minimum gate counts each generated strike',
          ([(n.on, n.off) for n in short_chord.notes[0]], short_chord.st[0]['short']),
          ([(0, 1000000), (0, 1000000)], 2))
    tuned = cfg_of('--map', '1=epiano', '--arp', '1=up:1/32:2')
    tuned.tuning = [100000 + 1000 * k for k in range(128)]
    conv = Conversion(smf_bytes(0, 32, [[(0, tempo(32000)), (0, [0x90, 60, 100]), (8, EOT)]]), tuned)
    ck.eq('Groove generated pitch chooses tuning entries',
          [x.split()[4] for x in lines(conv, 'groove-tuning') if x.split()[3] == 'on'], ['160000', '172000'])
    rpn = [(0, [0x90, 60, 100]), (0, [0xB0, 101, 0]), (0, [0xB0, 100, 0]),
           (0, [0xB0, 6, 12]), (0, [0xE0] + data14(12288)), (4, EOT)]
    ck.eq('Groove RPN range and bend precede coincident generated onset',
          [x.split()[4] for x in lines(convert(rpn, '--arp', '1=up:1/32', '--rpn'), 'groove-rpn')
           if x.split()[3] == 'on'], ['369994'])
    strum = [(0, [0x90, 60, 100]), (5, [0xE0] + data14(12288)), (20, [0x80, 60, 0]), (21, EOT)]
    ck.eq('Groove strummed onset retains source strike order at a coincident bend',
          [x for x in lines(convert(strum, '--chord', '1=0+4:5'), 'groove-strum-order') if x.startswith('5000000 ')],
          ['5000000 ch1 1 on 329628 25800', '5000000 ch1 0 pitch 277183', '5000000 ch1 1 pitch 349228'])
    filt = [(0, [0x90, 60, 100]), (0, [0x90, 64, 90]), (0, [0xB0, 74, 80]), (4, EOT)]
    ck.eq('Groove filter CC initializes generated voices after channel controls',
          [x for x in lines(convert(filt, '--arp', '1=chord:1/32:2', '--filter', '1=lp:1000:0.7'), 'groove-filter')
           if ' filter ' in x], ['0 ch1 0 filter 2000000 700', '0 ch1 1 filter 2000000 700'])
    same = cfg_of('--map', '1=epiano,5=bell,16=warm-pad', '--arp', '1=up:1/8', '--scale', '5=major',
                  '--mpe', 'upper:3')
    ck.eq('Groove independent channel scale and MPE remain available',
          (set(same.arps), set(same.scales), same.mpe), ({0}, {4}, {15: (14, 13, 12)}))
    for argv, option in ((('--arp', '1=up:1/8', '--arp-latch', '1,1'), '--arp-latch'),
                         (('--arp', '1=up:1/8', '--arp-seed', '1=0,1=2'), '--arp-seed'),
                         (('--chord', '1=0+4,1=0+7'), '--chord')):
        refuse('duplicate modifiers ' + option, lambda argv=argv: cfg_of('--map', '1=epiano', *argv), option)
    refuse('positive gate below nanosecond resolution',
           lambda: convert([(0, [0x90, 60, 100]), (4, EOT)], '--arp', '1=up:1/32:1:1e-100'), '--arp')
    huge = convert([(0, [0x90, 60, 100]), (4, EOT)], '--arp', '1=up:1/32:1:1000000000000000000')
    ck.eq('Groove gate exceeding native record range is refused before publication',
          huge.problems(), ['longer than the harness takes'])

    strikes = [(0, [0x90, 60, 100]), (6, [0x80, 60, 0]),
               (8, [0x90, 62, 90]), (14, [0x80, 62, 0]), (16, EOT)]
    randomized = convert(strikes, '--chord', '1=0+4+7:4', '--random', '1=7:50:10')
    # Literal xorshift32 pairs from seed7, reduced to cents/100 and velocity.
    expected = [(0, 60, Fraction(-2606, 100), 97), (2000000, 64, Fraction(2326, 100), 102),
                (4000000, 67, Fraction(1912, 100), 96), (8000000, 62, Fraction(-2116, 100), 87),
                (10000000, 66, Fraction(-2354, 100), 94), (12000000, 69, Fraction(-934, 100), 86)]
    ck.eq('Groove strummed random keeps each distinct onset draw pair',
          [(n.on, n.key, n.cents, n.vel) for n in randomized.notes[0]], expected)
    layered = convert(strikes, '--chord', '1=0+4+7:4', '--random', '1=7:50:10', '--map', '1=bell@0-127')
    ck.eq('Groove strummed random shares each draw pair across same-onset layers',
          [(n.on, n.key, n.cents, n.vel) for n in layered.notes[0]], [n for n in expected for _ in range(2)])
    unstrummed = convert(strikes, '--chord', '1=0+4+7', '--random', '1=7:50:10')
    ck.eq('Groove zero-spread chord keeps source-onset random sharing',
          [(n.cents, n.vel) for n in unstrummed.notes[0]],
          [(Fraction(-2606, 100), 97)] * 3 + [(Fraction(2326, 100), 92)] * 3)

    rpath=os.path.join(tmp,'groove-recipes.txt')
    with open(rpath,'w',encoding='utf-8',newline='\n') as f:f.write(C_RECIPES)
    added=compose.load_recipes(rpath);compose.RECIPES.update(added)
    try:
        poly=[(0,[0x90,60,100]),(5,[0xA0,60,127]),(8,[0x80,60,55]),(8,EOT)]
        conv=convert(poly,'--arp','1=up:1/32:2','--ctl','1=polyat:1',recipe='t37-all')
        ck.eq('Groove poly pressure follows original key across octaves',
              [x for x in lines(conv,'groove-poly') if x.startswith('5000000 ')],
              ['5000000 ch1 0 ctl 1 32768','5000000 ch1 1 ctl 1 32768'])
        rel=convert(poly,'--chord','1=0+4','--ctl','1=relvel:1',recipe='t37-all')
        ck.eq('Groove chord uses its physical key-up velocity on each copy',
              [x for x in lines(rel,'groove-release') if 'ctl 1 ' in x and x.startswith('8000000 ')],
              ['8000000 ch1 0 ctl 1 %d'%q7(55),'8000000 ch1 1 ctl 1 %d'%q7(55)])
        channel = [(0, [0x90, 60, 100]), (0, [0xD0, 127]), (0, [0xB0, 1, 64]), (8, EOT)]
        conv = convert(channel, '--arp', '1=up:1/32:2', '--ctl', '1=pressure:1,cc1:0', recipe='t37-all')
        ck.eq('Groove channel pressure and CC initialize each generated strike',
              [x for x in lines(conv, 'groove-channel') if ' ctl ' in x],
              ['0 ch1 0 ctl 0 %d' % q7(64), '0 ch1 0 ctl 1 32768',
               '4000000 ch1 1 ctl 0 %d' % q7(64), '4000000 ch1 1 ctl 1 32768'])
    finally:
        for name in added:del compose.RECIPES[name]
    control=[(0,[0x90,60,100]),(48,[0x80,60,0]),(48,[0x90,64,90]),(96,[0x80,64,0]),(96,EOT)]
    a=convert(control,division=96,us=500000)
    b=convert(control,'--arp','1=up:1/8:1:1',division=96,us=500000)
    ck.lines('Groove one-key rate/length control equals plain MIDI events',lines(a,'groove-plain'),lines(b,'groove-control'))
    if have:
        for fs in (48000,44100):
            pa=a.piece('selftest-groove-a',os.path.join(tmp,'groove-a-%d'%fs),'a.mid')
            pb=b.piece('selftest-groove-b',os.path.join(tmp,'groove-b-%d'%fs),'b.mid')
            pa.fs=pb.fs=fs
            ha=render(pa,os.path.join(tmp,'renders'),False)[1]
            hb=render(pb,os.path.join(tmp,'renders'),False)[1]
            ck.eq('Groove paired-rate native plain control %d'%fs,hb,ha)


def selftest_velocity_pool(ck, tmp):
    """regression: literal laws, selection/order, allocation and atomic bound refusals."""
    def refused(label, fn, text):
        try:
            fn()
        except ValueError as e:
            ck.ok(label, text in str(e), str(e))
        else:
            ck.ok(label, False, 'accepted')
    def data(velocities, *, key=60, channel=0):
        rows = []
        for i, v in enumerate(velocities):
            rows += [(i * 192, [0x90 + channel, key, v]), (i * 192 + 96, [0x80 + channel, key, 0])]
        return smf_bytes(0, 96, [rows + [(len(velocities) * 192, EOT)]])
    fade = ['--map', '1=epiano@0-127/1-80~', '--map', '1=epiano@0-127/74-127~']
    ck.eq('Crossfade one-velocity overlap is30/30',
          [crossfade_attenuation(64, 64, 64, up) for up in (True, False)], [30, 30])
    want = [(90, 6), (60, 12), (43, 20), (30, 30), (20, 43), (12, 60), (6, 90)]
    ck.eq('Crossfade seven-velocity overlap uses equal power',
          [(crossfade_attenuation(v, 74, 80, True), crossfade_attenuation(v, 74, 80, False))
           for v in range(74, 81)], want)
    with decimal.localcontext() as ctx:
        ctx.prec, ctx.rounding = 3, decimal.ROUND_DOWN
        ck.eq('Crossfade ignores ambient Decimal precision and rounding',
              [(crossfade_attenuation(v, 74, 80, True), crossfade_attenuation(v, 74, 80, False))
               for v in range(74, 81)], want)
    ck.eq('Crossfade ceil controller has exact integer contribution for all1201 levels',
          [crossfade_ctl(a) * 1200 // 32768 for a in range(1201)], list(range(1201)))
    ck.eq('Crossfade literal controller endpoints and midpoint',
          [crossfade_ctl(a) for a in (0, 1, 6, 30, 90, 1200)], [0, 28, 164, 820, 2458, 32768])
    for value in (-1, 1201, True, 1.0):
        refused('Crossfade refuses noninteger/out-of-range attenuation %r' % value,
                lambda value=value: crossfade_ctl(value), 'integer0..1200')
    cfg = cfg_of(*fade, '--voices', '1')
    conv = Conversion(data([73, 74, 77, 80, 81, 81]), cfg)
    ck.eq('Crossfade stores both maximum attenuation bounds', list(cfg.crossfades.values()), [90, 90])
    ck.eq('Crossfade literal surviving layers and attenuation',
          [(n.vel, n.patch[1][1], n._xfade_a) for n in conv.notes[0]],
          [(73, 0, 0), (74, 0, 6), (74, 1, 90), (77, 0, 30), (77, 1, 30),
           (80, 0, 90), (80, 1, 6), (81, 1, 0), (81, 1, 0)])
    p = conv.piece('velocity', os.path.join(tmp, 'velocity'), 'velocity.mid')
    p.write()
    with open(os.path.join(p.dir, 'ch1.tl'), encoding='utf-8') as f:
        events = [line.split('#', 1)[0].split() for line in f if line.strip() and not line.startswith('#')]
    ons = [i for i, row in enumerate(events) if row[3] == 'on']
    ck.eq('Crossfade writes one explicit ctl before every on including repeated zero', len(ons), 9)
    ck.ok('Crossfade ctl has the same time/voice and immediately precedes its on',
          all(events[i-1][:3] == events[i][:3] and events[i-1][3] == 'ctl' for i in ons))
    ck.eq('Crossfade repeated zero is retained', [events[i-1][5] for i in ons[-2:]], ['0', '0'])
    p.validate_crossfades({'ch1z1': 1110, 'ch1z2': 1110})
    ck.ok('Crossfade level plus maximum exactly1200 is accepted', True)
    refused('Crossfade final leveller clamp refuses by patch name',
            lambda: p.validate_crossfades({'ch1z1': 1111}), 'crossfade patch ch1z1')
    stream_before = read_bytes(os.path.join(p.dir, 'ch1.tl'))
    refused('Crossfade write refuses before opening stream',
            lambda: p.write({'ch1z1': 1111}), 'maximum attenuation90')
    ck.eq('Crossfade refused write preserves existing stream', read_bytes(os.path.join(p.dir, 'ch1.tl')),
          stream_before)

    def make_patch(name, levels, vector=False):
        q = MidiPiece(name, 120, 4, [])
        q.dir = os.path.join(tmp, name)
        car, mod, *_ = compose.recipe('epiano')
        operators = [list(car) for _ in levels]
        for op, level in zip(operators, levels):
            op[1] = level
        extra = {'ops': len(levels), 'carriers': (1 << len(levels)) - 1, 'edges': (),
                 'more': operators[2:]}
        if vector:
            extra['vector'] = (16384, 16384)
        q.patch('test', operators[0], operators[1], '', extra)
        q.crossfades = {'test': 90}
        q.stream('s', 'bounds')
        q.add_voices('test', 1, 'bounds')
        q.length_ns = 1000000
        return q
    for k, lower in ((2, 1), (3, 36), (4, 61)):
        q = make_patch('bound%d' % k, [lower] * k)
        q.validate_crossfades()
        ck.ok('Crossfade K%d native sum threshold is accepted' % k, True)
        refused('Crossfade K%d below native sum threshold refuses' % k,
                lambda q=q, lower=lower: q.validate_crossfades({'test': lower - 1}), 'unclamped')
    q = make_patch('offset', [100, 1100])
    refused('Crossfade refuses a different carrier clipping in the common shift',
            lambda: q.validate_crossfades({'test': 210}), 'carrier1')
    q = make_patch('convex', [0] * 4, True)
    q.validate_crossfades()
    ck.ok('Crossfade convex vector permits zero base level', True)

    car, mod, *_ = compose.recipe('epiano')
    ops = [car, mod]
    for kind, value in (('shape', (0, 'hard', 'pre')), ('shape', (0, 'hard')),
                        ('filter', ('lp', 1000000, 1000)), ('fenv', (0, 0, 0, 0, 0, 0)),
                        ('formant', {}), ('amp', (0, 0, 0, 0))):
        refused('Crossfade refuses post-carrier stage %s %r' % (kind, value),
                lambda kind=kind, value=value: crossfade_settings('named', ops, {kind: value}, (), FS), kind)
    refused('Crossfade refuses filter override even without sounding notes',
            lambda: cfg_of(*fade, '--filter', '1=lp:1000:1'), '--filter')
    for target, extra in (
            ('level0', {'mod': {0: ('ctl0', 'level0', 1, 0)}}),
            ('level key scaling', {'kstype': {0: ('level', '-', '-')}, 'ks': {0: 1}}),
            ('LFO level', {'lfo': (0, 1000, 0, 0, 0, 1, 0)}),
            ('LFO level', {'lfo': (0, 1000, 0, 0, 0, 0, 0),
                           'mod': {0: ('ctl0', 'lfo_level', 1, 0)}})):
        refused('Crossfade refuses dynamic %s %r' % (target, extra),
                lambda extra=extra: crossfade_settings('named', ops, extra, (), FS), target)
    extra = {'mod': {0: ('ctl0', 'level0', 0, 0), 2: ('ctl3', 'pitch0', 0, 3)}}
    result, input_ = crossfade_settings('reserve', ops, extra, (1, 2), FS)
    ck.eq('Crossfade reserves zero slots and configured ctl destinations',
          (input_, result['mod'][1]), (4, ('ctl4', 'level0', 1200, 0)))
    ck.eq('Crossfade slot allocation leaves caller metadata unchanged',
          extra, {'mod': {0: ('ctl0', 'level0', 0, 0), 2: ('ctl3', 'pitch0', 0, 3)}})
    mods, macros = compose.macro_budget({}, {}, [('pitch', 0, [('pitch0', 100)], False, None)], ops, {}, FS)
    result, input_ = crossfade_settings('macro', ops, {'mod': mods, 'macros': macros}, (), FS)
    ck.eq('Crossfade preserves macro input and ownership',
          (input_, result['mod'][0], result['mod'][1], result['macros']),
          (1, ('ctl0', 'pitch0', 100, 3), ('ctl1', 'level0', 1200, 0), macros))
    ctlcfg = cfg_of(*fade, '--ctl', '1=default', '--ctl', '1=cc7:4')
    ck.eq('Crossfade reserves all configured inputs including unused rand',
          configured_pool(ctlcfg, (0, ('z', 0))).crossfade_input, 7)
    for lfo in ((0, 1000, 0, 0, 0, 100, 0, 0, 3, 0, 3), (0, 1000, 0, 0, 0, 100, 0, 0, 3, 2, 3)):
        result, _ = crossfade_settings('inactive-level', ops, {'lfo': lfo}, (), FS)
        ck.eq('Crossfade permits disabled or noncarrier-only LFO level ' + repr(lfo),
              result['lfo'], lfo)
    lone = cfg_of('--map', '1=epiano@0-127/1-63~')
    ck.eq('Crossfade marked disjoint zone uses maximum0',
          lone.crossfades, {(0, ('z', 0)): 0})
    refused('Crossfade names exhausted inputs',
            lambda: crossfade_settings('full-input', ops, {}, range(8), FS), 'full-input')
    refused('Crossfade names exhausted slots',
            lambda: crossfade_settings('full-slot', ops,
                {'mod': {i: ('ctl0', 'pitch0', 0, 0) for i in range(8)}}, (), FS), 'full-slot')
    pair_ops = [list(car), list(car)]
    routed = {'ops': 2, 'carriers': 3, 'edges': ((0, 1, 256, 'am'),)}
    result, _ = crossfade_settings('outgoing', pair_ops, routed, (), FS)
    ck.eq('Crossfade allows outgoing non-PM edges and allocates each carrier',
          result['mod'], {0: ('ctl0', 'level0', 1200, 0), 1: ('ctl0', 'level1', 1200, 0)})

    for argv, text in (
            (['--map', '1=epiano@0-127~'], 'requires explicit /VLO-VHI'),
            (['--map', '1=epiano@0-127/0-80'], 'velocity range'),
            (['--map', '1=epiano@0-127/90-80'], 'velocity range'),
            (['--map', '1=epiano@0-127/1-128'], 'velocity range'),
            (['--map', '1=epiano@0-127/1-80~', '--map', '1=epiano@0-127/74-127'], 'both'),
            (['--map', '1=epiano@0-127/1-80~', '--map', '1=epiano@0-127/1-80~'], 'equal'),
            (['--map', '1=epiano@0-127/1-100~', '--map', '1=epiano@0-127/20-80~'], 'nested'),
            (fade + ['--map', '1=epiano@60-60/77-77'], 'three'),
            (fade + ['--mono', '1=legato'], 'key'),
            (fade + ['--map', '1=epiano', '--mpe', 'lower:1'], '--mpe'),
            (['--map', '1=epiano', '--mpe', 'lower:1', '--pool', '3'], ''),
            (['--map', '1=epiano', '--pool', '0'], '--pool'),
            (['--map', '1=epiano', '--pool', '65'], '--pool'),
            (['--map', '1=epiano', '--reserve', 'ch1=1'], 'require --pool'),
            (['--map', '1=epiano', '--pool', '2', '--reserve', 'epiano=1'], 'unknown'),
            (['--map', '1=epiano', '--pool', '2', '--reserve', 'ch1=1,ch1=1'], 'twice'),
            (['--map', '1=epiano', '--pool', '2', '--limit', 'ch1=65'], 'INTEGER'),
            (['--map', '1=epiano', '--pool', '2', '--limit', 'ch1=-1'], 'INTEGER'),
            (['--map', '1=epiano', '--pool', '2', '--reserve', 'ch1=2', '--limit', 'ch1=1'], 'exceeds'),
            (['--map', '1=epiano', '--pool', '1', '--reserve', 'ch1=2'], 'total'),
            (['--map', '1=epiano', '--mono', '1=legato', '--pool', '2', '--limit', 'ch1=2'], 'mono')):
        refused('Velocity/pool refusal: ' + ' '.join(argv), lambda argv=argv: cfg_of(*argv), text)
    for exists in (False, True):
        destination = os.path.join(tmp, 'bad-velocity-grammar%d' % exists)
        if exists:
            os.makedirs(destination)
            with open(os.path.join(destination, 'session.tl'), 'wb') as f:
                f.write(b'owner grammar sentinel\n')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(['unopened.mid', '--map', '1=epiano@0-127~', '--out', destination])
        ck.ok('Crossfade missing velocity grammar refuses before input/output, exists=%s' % exists,
              status == 1 and 'requires explicit /VLO-VHI' in err.getvalue())
        ck.eq('Crossfade grammar refusal preserves destination, exists=%s' % exists,
              read_bytes(os.path.join(destination, 'session.tl')) if exists else os.path.exists(destination),
              b'owner grammar sentinel\n' if exists else False)

    # Random draws must still include the strike whose velocity selection fails.
    random_args = ['--random', '1=7:50:10']
    broad = Conversion(data([100, 100, 100]), cfg_of('--map', '1=epiano', *random_args))
    selected = Conversion(data([100, 100, 100]), cfg_of('--map', '1=epiano@0-127/100-127', *random_args))
    ck.eq('Velocity selection follows random draws and preserves later values',
          [(n.on, n.cents, n.vel) for n in selected.notes[0]],
          [(n.on, n.cents, n.vel) for n in broad.notes[0] if n.vel >= 100])
    layered = Conversion(data([63, 64, 65]), cfg_of('--map', '1=epiano@0-127/64-64',
                         '--map', '1=epiano@0-127/64-65', '--unison', '1=2:0'))
    ck.eq('Velocity skip counts source strikes before unison',
          (layered.st[0]['notes'], layered.st[0]['velocity_skipped'], len(layered.notes[0])), (2, 1, 6))
    ck.ok('Velocity skip appears in summary', 'velocity-zone strikes skipped 1' in
          '\n'.join(layered.summary('v', 'v.mid')))
    old = Conversion(data([1, 127]), cfg_of('--map', '1=epiano@0-127'))
    full = Conversion(data([1, 127]), cfg_of('--map', '1=epiano@0-127/1-127'))
    ck.eq('Explicit full velocity zone keeps old events', old.out[0][0][4], full.out[0][0][4])
    ck.eq('Unmarked overlapping zones layer without ctl',
          [event[1] for event in layered.out[0]].count(4), 0)

    order = cfg_of('--map', '2=epiano@0-127', '--map', '1=epiano', '--map', '2=epiano',
                   '--program', '7=organ,2=epiano', '--kit', '10=40:hat,36:kick', '--pool', '2')
    ck.eq('Pool map/program/kit order follows declarations',
          [patch_spec(order, key)[0] for key in order.patch_order],
          ['ch2z1', 'ch1', 'ch1p7', 'ch1p2', 'ch2', 'ch2p7', 'ch2p2', 'ch10k36', 'ch10k40'])
    simultaneous = smf_bytes(0, 96, [[(0, [0x90, 60, 100]), (0, [0x91, 60, 100]),
        (0, [0x92, 60, 100]), (96, [0x80, 60, 0]), (96, [0x81, 60, 0]),
        (96, [0x82, 60, 0]), (192, EOT)]])
    split = Conversion(simultaneous, cfg_of('--map', '2=epiano,1=epiano,3=epiano', '--pool', '2'))
    ck.eq('Pool zero cap follows map order and does not call pick',
          split.pool_caps, {(1, None): 1, (0, None): 1, (2, None): 0})
    crowded = smf_bytes(0, 96, [[(0, [0x90, 60, 100]), (0, [0x90, 64, 100]),
        (0, [0x91, 60, 100]), (0, [0x91, 64, 100]), (96, [0x80, 60, 0]), (96, [0x80, 64, 0]),
        (96, [0x81, 60, 0]), (96, [0x81, 64, 0]), (192, EOT)]])
    dealt = Conversion(crowded, cfg_of('--map', '2=epiano,1=epiano', '--pool', '3'))
    ck.eq('Pool deals a second round in declaration order',
          dealt.pool_caps, {(1, None): 2, (0, None): 1})
    limited = Conversion(crowded, cfg_of('--map', '2=epiano,1=epiano', '--pool', '4', '--limit', 'ch2=1'))
    ck.eq('Pool never exceeds explicit limit or measured demand',
          limited.pool_caps, {(1, None): 1, (0, None): 2})
    ck.eq('Pool zero cap counts a lost source strike once',
          (split.st[2]['notes'], split.st[2]['pool_skipped'], split.notes[2], split.pools[2]), (0, 1, [], {}))
    ck.ok('Pool zero cap appears in summary', 'pool voice notes skipped 1' in
          '\n'.join(split.summary('pool', 'pool.mid')))
    reserve = Conversion(simultaneous, cfg_of('--map', '1=epiano,2=epiano,3=epiano,4=epiano',
        '--pool', '3', '--reserve', 'ch4=2'))
    ck.eq('Unused explicit reserve consumes capacity without an emitted patch',
          (reserve.pool_caps, reserve.pools[3]),
          ({(0, None): 1, (1, None): 0, (2, None): 0, (3, None): 2}, {}))
    surviving = Conversion(data([100]), cfg_of('--map', '1=epiano@0-127', '--map', '1=epiano@0-127',
        '--unison', '1=2:0', '--pool', '1'))
    ck.eq('Pool skips expanded copies but retains source count when another layer survives',
          (surviving.st[0]['notes'], surviving.st[0]['pool_skipped'], len(surviving.notes[0])), (1, 2, 2))
    ck.eq('Positive pool cap retains partial unison stealing', surviving.st[0]['steals'], 1)

    # The overridden release is300ms: note-off at500ms frees it at800ms exactly.
    release = smf_bytes(0, 1000, [[(0, [0x90, 60, 100]), (1000, [0x80, 60, 0]),
        (1600, [0x90, 62, 100]), (2000, [0x80, 62, 0]), (3000, EOT)]])
    relcfg = cfg_of('--map', '1=epiano c_r=-50', '--pool', '8')
    exact = Conversion(release, relcfg)
    ck.eq('Pool release intervals process ends before starts at exact tie', exact.pool_caps[(0, None)], 1)
    overlap = smf_bytes(0, 1000, [[(0, [0x90, 60, 100]), (1000, [0x80, 60, 0]),
        (1599, [0x90, 62, 100]), (2000, [0x80, 62, 0]), (3000, EOT)]])
    ck.eq('Pool includes release before the exact tie',
          Conversion(overlap, relcfg).pool_caps[(0, None)], 2)

    # Exercise private staging without a native render; a late validation failure
    # must not touch either a preexisting destination or a nonexistent one.
    saved_render = globals()['render']
    try:
        for exists in (False, True):
            q = make_patch('atomic%d' % exists, [100, 100])
            if exists:
                os.makedirs(q.dir)
                with open(os.path.join(q.dir, 'session.tl'), 'wb') as f:
                    f.write(b'owner sentinel\n')
            target = q.dir
            observed = []
            def fake_render(piece, renders, echo):
                observed.append(piece.dir)
                piece.write({'test': 100})
                piece.write({'test': 0})          # a later leveller pass crosses B2
            globals()['render'] = fake_render
            refused('Crossfade late refusal is atomic, destination exists=%s' % exists,
                    lambda: render_crossfade(q, False), 'unclamped')
            ck.ok('Crossfade private artifacts are removed, destination exists=%s' % exists,
                  len(observed) == 1 and not os.path.exists(observed[0]) and q.dir == target)
            ck.eq('Crossfade target stays unchanged, destination exists=%s' % exists,
                  read_bytes(os.path.join(target, 'session.tl')) if exists else os.path.exists(target),
                  b'owner sentinel\n' if exists else False)
    finally:
        globals()['render'] = saved_render


def selftest():
    """Small MIDI files built in memory, converted and checked against times and lines worked out
    by hand; with the harness built, also rendered, replayed, converted again and written by hand
    to the same hash."""
    ck, have = Checks(), os.path.exists(compose.H)
    tmp = tempfile.mkdtemp(prefix='midi2tl-selftest-')
    try:
        selftest_units(ck)
        selftest_a(ck, tmp, have)
        selftest_b(ck, tmp, have)
        selftest_d(ck, tmp, have)
        selftest_f(ck, tmp)
        selftest_g(ck, tmp)
        selftest_faults(ck)
        selftest_cli(ck, tmp)
        selftest_modes(ck, tmp, have)
        selftest_mode_faults(ck, tmp)
        selftest_ctl(ck, tmp, have)
        selftest_mpe(ck, tmp, have)
        selftest_scale(ck, tmp, have)
        selftest_groove(ck, tmp, have)
        selftest_arp_conditions(ck)
        selftest_velocity_pool(ck, tmp)
    except (Exception, SystemExit) as e:
        traceback.print_exc()
        ck.ok('the self-test ran to its end', False, '%s: %s' % (type(e).__name__, e))
    if ck.failed:
        print('selftest: %d of %d checks FAILED: %s; the files are in %s' % (
            len(ck.failed), ck.n, ', '.join(ck.failed), tmp))
        return 1
    shutil.rmtree(tmp)
    print('selftest: %d checks passed%s' % (ck.n, '' if have else '; the harness %s is not built, so nothing was'
                                            ' rendered (build it: make -s SINE_BITS=12 EXP_BITS=10'
                                            ' PHASE64=1 ENV64=1)' % rel(compose.H)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
