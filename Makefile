# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
CC ?= cc
HOSTCC ?= cc
FS ?= 48000
OPT ?= -O2
SINE_BITS ?= 12
EXP_BITS ?= 10
PHASE64 ?= 1
ENV64 ?= 1
VARIANT ?=
EXTRA ?=
TAG := s$(SINE_BITS)e$(EXP_BITS)p$(PHASE64)v$(ENV64)$(VARIANT)
B := build/$(TAG)
WARN := -std=c99 -pedantic -Wall -Wextra -Wshadow -ffp-contract=off
LIBWARN := $(WARN) -Werror -Wsign-conversion
DEFS := -DFM_SINE_BITS=$(SINE_BITS) -DFM_EXP_BITS=$(EXP_BITS) -DFM_PHASE64=$(PHASE64) -DFM_ENV64=$(ENV64) $(EXTRA)
INCLUDES := -Isrc/host -I$(B) -I.
HDEFS := -DSP_BINAURAL_H='"$(B)/sp_binaural_tables.h"'
OBJS := $(addprefix $(B)/,fm_a1.o gen.o space.o fx.o field.o)

all: $(B)/harness
$(B):
	mkdir -p $@
$(B)/gen_tables_int: src/host/gen_tables_int.c vendor/fp_math.h | $(B)
	$(HOSTCC) $(WARN) $(OPT) $(EXTRA) -o $@ $<
$(B)/gen_tables: src/host/gen_tables.c | $(B)
	$(HOSTCC) $(WARN) $(OPT) -o $@ $< -lm
$(B)/gen_binaural_int: src/space/gen_binaural_int.c vendor/fp_math.h | $(B)
	$(HOSTCC) $(WARN) $(OPT) $(EXTRA) -o $@ $<
.PHONY: table-bounds
table-bounds:
	@test $(SINE_BITS) -ge 2 -a $(SINE_BITS) -le 14 -a $(EXP_BITS) -ge 1 -a $(EXP_BITS) -le 11 || { echo 'Table sizes require sine 2..14 and exp2 1..11 bits' >&2; exit 1; }
$(B)/fm_tables.h: $(B)/gen_tables_int | table-bounds
	$< $(SINE_BITS) $(EXP_BITS) > $@.tmp && mv $@.tmp $@
$(B)/sp_binaural_tables.h: $(B)/gen_binaural_int
	$< > $@.tmp && mv $@.tmp $@
$(B)/fm_a1.o: src/host/fm_a1.c src/host/fm_a1.h vendor/fp_math.h $(B)/fm_tables.h
	$(CC) $(LIBWARN) $(OPT) $(DEFS) $(INCLUDES) -c -o $@ $<
$(B)/gen.o: src/gen/gen.c src/gen/gen.h vendor/fp_math.h | $(B)
	$(CC) $(LIBWARN) $(OPT) $(DEFS) $(INCLUDES) -c -o $@ $<
$(B)/space.o: src/space/space.c src/space/space.h src/fx/fx.h src/host/fm_a1.h | $(B)
	$(CC) $(LIBWARN) $(OPT) $(DEFS) $(INCLUDES) -c -o $@ $<
$(B)/fx.o: src/fx/fx.c src/fx/fx.h src/host/fm_a1.h | $(B)
	$(CC) $(LIBWARN) $(OPT) $(DEFS) $(INCLUDES) -c -o $@ $<
$(B)/field.o: src/space/field.c src/space/field.h src/space/space.h src/fx/fx.h src/host/fm_a1.h vendor/fp_math.h | $(B)
	$(CC) $(LIBWARN) $(OPT) $(DEFS) $(INCLUDES) -c -o $@ $<
$(B)/harness: src/host/harness.c src/host/version.h src/host/ref_double.c src/host/fx_ref.c src/host/field_ref.c src/host/ref_double.h src/host/fx_ref.h src/host/field_ref.h src/host/params.def src/gen/params.def $(OBJS) $(B)/sp_binaural_tables.h
	$(CC) $(WARN) $(OPT) $(DEFS) $(HDEFS) $(INCLUDES) -o $@ src/host/harness.c src/host/ref_double.c src/host/fx_ref.c src/host/field_ref.c $(OBJS) -lm
portable:
	$(MAKE) EXTRA=-DFP_MATH_FORCE_PORTABLE VARIANT=-portable
ubsan:
	$(MAKE) OPT='-O1 -g -fsanitize=undefined -fno-sanitize-recover=all' VARIANT=-ubsan
check: all
	bash tests/native.sh
macos:
	$(MAKE) -C apps/macos
clean:
	rm -rf build
.PHONY: all portable ubsan check macos clean

quick: all check-catalog
	python3 -I -B tests/tl_document.py --harness $(abspath $(B)/harness)
	python3 -I -B tests/tl_document_native.py
	node --test tests/tl_document.test.mjs
	python3 -B tests/cli_safety.py
	python3 -B tests/decode_cli.py
	python3 -B tests/transcribe_robust.py
	python3 -B tests/wav2tl_cli.py
	python3 -B tests/decoding_docs.py
	python3 -B tests/repo_hygiene.py
	$(B)/harness --check
	$(B)/harness --play tests/golden/48000/sine.tl --metrics
	python3 tools/music/compose.py --selftest-perf
.PHONY: quick

# Catalog export needs the native harness; the site client needs prebuilt Wasm
# assets and serves only loopback HTTP. Neither target downloads dependencies.
check-catalog: all
	HARNESS="$(abspath $(B)/harness)" python3 -I -B tests/export_library.py
check-agent:
	node tests/agent_client.mjs
.PHONY: check-catalog check-agent

wasm:
	python3 tools/build_wasm.py
.PHONY: wasm

SONG_JOBS ?= 1
TUNES_ROOT ?= $(CURDIR)
SONG_CATALOG ?= $(if $(wildcard $(TUNES_ROOT)/library.json),$(TUNES_ROOT)/library.json,$(TUNES_ROOT)/music/library.json)
ifneq ($(filter web songs web/songs.json,$(MAKECMDGOALS)),)
SONG_INPUTS := $(shell python3 tools/export_library.py --root "$(TUNES_ROOT)" --catalog "$(SONG_CATALOG)" --list-inputs || printf '\nINVALID_SONG_INPUTS\n')
ifneq ($(filter INVALID_SONG_INPUTS,$(SONG_INPUTS)),)
$(error Cannot read song catalog inputs)
endif
SONG_OUTPUT_VALID := $(shell python3 tools/export_library.py --root "$(TUNES_ROOT)" --catalog "$(SONG_CATALOG)" --check-output >/dev/null 2>&1 && printf valid)
ifneq ($(SONG_OUTPUT_VALID),valid)
.PHONY: web/songs.json
endif
endif
web/songs.json: $(SONG_CATALOG) tools/export_library.py tools/tl_document.py $(SONG_INPUTS) $(B)/harness
	python3 tools/export_library.py --root "$(TUNES_ROOT)" --catalog "$(SONG_CATALOG)" --harness $(B)/harness --jobs $(SONG_JOBS)
songs: web/songs.json
.PHONY: songs

spatial-web:
	python3 tools/build_spatial.py
.PHONY: spatial-web

$(B)/spatial-oracle: tests/spatial_oracle.c bindings/native/jottune_bridge.c bindings/native/jottune_bridge.h src/host/harness.c $(OBJS) $(B)/sp_binaural_tables.h
	$(CC) $(WARN) $(OPT) $(DEFS) $(HDEFS) $(INCLUDES) -o $@ tests/spatial_oracle.c src/host/ref_double.c src/host/fx_ref.c src/host/field_ref.c $(OBJS) -lm

check-spatial: spatial-web $(B)/spatial-oracle
	node tests/spatial.test.mjs --oracle $(B)/spatial-oracle
.PHONY: check-spatial

# The browser runner always includes desktop and named phone emulation profiles.
# Supply an existing player; no server, browser or package is installed here.
check-browser:
	@test -n "$(PLAYER_URL)" || { echo 'Set PLAYER_URL to a served player URL.' >&2; exit 2; }
	node tests/browser.mjs "$(PLAYER_URL)" $(BROWSER_FLAGS)
.PHONY: check-browser

check-interchange: all
	JOTTUNE_TEST_HARNESS="$(abspath $(B)/harness)" python3 tests/midi_export.py
	python3 tests/interchange.py --harness "$(B)/harness"
	python3 tests/sample_export.py --harness "$(B)/harness"
	python3 tests/receiver_roundtrip.py --harness "$(B)/harness" $(RECEIVER_FLAGS)
.PHONY: check-interchange

web: spatial-web
	python3 tools/build_analysis.py
	$(MAKE) songs
	python3 tools/package_web.py
.PHONY: web
