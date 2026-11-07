# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# Build the macOS player, instrument controls, recipes and native bridge.
# Dead stripping excludes the host reference models from the app.
REPO := $(abspath $(dir $(abspath $(lastword $(MAKEFILE_LIST))))/../..)
HERE := $(REPO)/apps/macos
B := $(HERE)/build
SRC := $(REPO)
KNOBS := SINE_BITS=12 EXP_BITS=10 PHASE64=1 ENV64=1
TAG := s12e10p1v1-macos
LIB := $(REPO)/build/$(TAG)
DEFS := -DFM_SINE_BITS=12 -DFM_EXP_BITS=10 -DFM_PHASE64=1 -DFM_ENV64=1
HDEFS := -DSP_BINAURAL_H='"$(LIB)/sp_binaural_tables.h"'
CC ?= cc
CFLAGS := -std=c99 -O2 -Wall -Wextra -ffp-contract=off
OBJS := $(LIB)/fm_a1.o $(LIB)/space.o $(LIB)/fx.o $(LIB)/field.o $(LIB)/gen.o
HARNESS := $(LIB)/harness
APP := $(B)/IntegerFM.app
SWIFTC ?= swiftc
SWIFT_DEFS ?=
SWIFT := $(sort $(wildcard $(HERE)/App/*.swift))
HEADREV := source checkout
export MACOSX_DEPLOYMENT_TARGET := 14.0
export SDKROOT ?= $(shell xcrun --sdk macosx --show-sdk-path)

all: $(APP)/Contents/MacOS/IntegerFM recipes

# The distributable has no source checkout or external Python dependency.
# Build-time recipe expansion remains shared with the developer app.
standalone: all
	rm -rf "$(B)/JotTune Synth.app"
	ditto $(APP) "$(B)/JotTune Synth.app"
	@for key in FMSRepoRoot FMSRecipesDir FMSRecipeTool FMSMusicDir FMSHarness FMSHeadCommit; do \
	    /usr/libexec/PlistBuddy -c "Delete :$$key" "$(B)/JotTune Synth.app/Contents/Info.plist"; done
	/usr/libexec/PlistBuddy -c 'Set :CFBundleName JotTune Synth' "$(B)/JotTune Synth.app/Contents/Info.plist"
	mkdir -p "$(B)/JotTune Synth.app/Contents/Resources/recipes"
	cp $(B)/recipes/*.tl "$(B)/JotTune Synth.app/Contents/Resources/recipes/"
	codesign --force -s - "$(B)/JotTune Synth.app"

standalone-check: standalone
	mkdir -p $(B)/standalone-check
	printf 'localfilescheck %s\n' '$(REPO)/tests/golden/48000/bell.tl' > $(B)/standalone-check/steps.txt
	env -u FMS_REPO_ROOT -u FMS_RECIPES_DIR FMS_SCRIPT=$(B)/standalone-check/steps.txt \
	    FMS_SCRIPT_OUT=$(B)/standalone-check/run "$(B)/JotTune Synth.app/Contents/MacOS/IntegerFM"

$(B):
	mkdir -p $@

# Build current source, with no external checkout or history dependency.
native: | $(B)
	$(MAKE) -C $(REPO) $(KNOBS) VARIANT=-macos

NATIVE_SRC := $(wildcard $(REPO)/src/*/*.c $(REPO)/src/*/*.h $(REPO)/src/*/*.def $(REPO)/vendor/*.h)
SHARED_BRIDGE := $(REPO)/bindings/native/jottune_bridge.c $(REPO)/bindings/native/jottune_bridge.h $(REPO)/bindings/native/fms_midi.c
$(B)/lib.stamp: $(NATIVE_SRC) $(REPO)/Makefile $(HERE)/Makefile | native
	touch $@

$(B)/jottune_bridge.o: $(HERE)/bridge/jottune_bridge.c $(HERE)/bridge/jottune_bridge.h $(SHARED_BRIDGE) $(B)/lib.stamp
	$(CC) $(CFLAGS) $(DEFS) $(HDEFS) -I$(HERE)/bridge -I$(SRC)/src/host -I$(LIB) -c -o $@ $<

# The live MIDI translator (regression); the mutants are check (a)'s controls:
# 1 wrong velocity, 2 dropped note-off, 3 wrong ctl map, 4 wrong MPE bend range.
$(B)/fms_midi.o: $(REPO)/bindings/native/fms_midi.c $(HERE)/bridge/fms_midi.c $(HERE)/bridge/jottune_bridge.h | $(B)
	$(CC) $(CFLAGS) -I$(HERE)/bridge -c -o $@ $<

$(B)/fms_midi_m%.o: $(REPO)/bindings/native/fms_midi.c $(HERE)/bridge/fms_midi.c $(HERE)/bridge/jottune_bridge.h | $(B)
	$(CC) $(CFLAGS) -DFMS_MIDI_MUTANT=$* -I$(HERE)/bridge -c -o $@ $<

$(B)/lines_model: $(HERE)/App/PatchLines.swift $(HERE)/App/Params.swift $(HERE)/tests/patch_model.swift $(HERE)/bridge/jottune_bridge.h $(B)/jottune_bridge.o $(B)/fms_midi.o
	$(SWIFTC) $(SWIFT_DEFS) -O -target arm64-apple-macos14.0 -parse-as-library -import-objc-header $(HERE)/bridge/jottune_bridge.h \
	    $(HERE)/App/PatchLines.swift $(HERE)/App/Params.swift $(HERE)/tests/patch_model.swift $(B)/jottune_bridge.o $(B)/fms_midi.o $(OBJS) -Xlinker -dead_strip -o $@

$(B)/files_model: $(HERE)/App/PatchLines.swift $(HERE)/App/Params.swift $(HERE)/App/Recipes.swift $(HERE)/tests/file_model.swift $(B)/jottune_bridge.o $(B)/fms_midi.o
	$(SWIFTC) $(SWIFT_DEFS) -O -target arm64-apple-macos14.0 -parse-as-library -import-objc-header $(HERE)/bridge/jottune_bridge.h \
	    $(HERE)/App/PatchLines.swift $(HERE)/App/Params.swift $(HERE)/App/Recipes.swift $(HERE)/tests/file_model.swift $(B)/jottune_bridge.o $(B)/fms_midi.o $(OBJS) -framework SwiftUI -Xlinker -dead_strip -o $@

$(B)/library_model: $(HERE)/App/RecordLibrary.swift $(HERE)/tools/library_test.swift
	$(SWIFTC) -O -target arm64-apple-macos14.0 -parse-as-library $^ -o $@

$(B)/local_store_model: $(HERE)/App/LocalRecordStore.swift $(HERE)/tests/local_store_model.swift
	$(SWIFTC) -O -target arm64-apple-macos14.0 -parse-as-library $^ -o $@

$(B)/fms_verify: $(HERE)/bridge/fms_verify.c $(HERE)/bridge/jottune_bridge.h $(B)/jottune_bridge.o $(B)/fms_midi.o
	$(CC) $(CFLAGS) -I$(HERE)/bridge -o $@ $< $(B)/jottune_bridge.o $(B)/fms_midi.o $(OBJS) -Wl,-dead_strip

$(B)/fms_verify_m%: $(HERE)/bridge/fms_verify.c $(HERE)/bridge/jottune_bridge.h $(B)/jottune_bridge.o $(B)/fms_midi_m%.o
	$(CC) $(CFLAGS) -I$(HERE)/bridge -o $@ $< $(B)/jottune_bridge.o $(B)/fms_midi_m$*.o $(OBJS) -Wl,-dead_strip

$(B)/Info.plist: $(HERE)/Info.plist.in $(HERE)/Makefile $(B)/fms_verify
	sed -e 's|@FMSRepoRoot@|$(REPO)|' -e 's|@FMSRecipesDir@|$(B)/recipes|' \
	    -e 's|@FMSRecipeTool@|$(HERE)/tools/make_recipes.py|' -e 's|@FMSMusicDir@|$(REPO)/tools/music|' \
	    -e 's|@FMSHarness@|$(HARNESS)|' -e 's|@FMSHeadCommit@|$(HEADREV)|' -e "s|@FMSEngineLine@|`$(B)/fms_verify --engine`|" $< > $@

$(B)/gen-params.txt: $(B)/lib.stamp
	$(HARNESS) --gen-params > $@

$(B)/params.txt: $(B)/lib.stamp
	$(HARNESS) --params > $@

$(APP)/Contents/MacOS/IntegerFM: $(HERE)/FEATURES.md $(HERE)/PrivacyInfo.xcprivacy $(REPO)/LICENSE $(REPO)/apps/NOTICE.txt $(B)/gen-params.txt $(B)/params.txt $(SWIFT) $(HERE)/bridge/jottune_bridge.h $(B)/jottune_bridge.o $(B)/fms_midi.o $(B)/Info.plist
	mkdir -p $(APP)/Contents/MacOS $(APP)/Contents/Resources
	cp $(B)/Info.plist $(APP)/Contents/Info.plist
	cp $(B)/gen-params.txt $(APP)/Contents/Resources/
	cp $(B)/params.txt $(APP)/Contents/Resources/
	cp $(REPO)/LICENSE $(REPO)/apps/NOTICE.txt $(APP)/Contents/Resources/
	cp $(HERE)/PrivacyInfo.xcprivacy $(APP)/Contents/Resources/
	if [ -f $(HERE)/FEATURES.md ]; then cp $(HERE)/FEATURES.md $(APP)/Contents/Resources/; fi
	$(SWIFTC) $(SWIFT_DEFS) -O -target arm64-apple-macos14.0 -parse-as-library -module-name IntegerFM \
	    -import-objc-header $(HERE)/bridge/jottune_bridge.h $(SWIFT) $(B)/jottune_bridge.o $(B)/fms_midi.o $(OBJS) \
	    -framework SwiftUI -framework AppKit -framework AVFoundation -framework Accelerate -framework CoreMIDI \
	    -Xlinker -dead_strip -o $@
	codesign --force -s - $(APP)

recipes: $(B)/recipes/.done

$(B)/recipes/.done: $(B)/lib.stamp $(B)/fms_verify $(HERE)/tools/make_recipes.py $(REPO)/tools/music/compose.py $(REPO)/data/recipes.txt $(REPO)/data/tables.txt
	python3 -I -B $(HERE)/tools/make_recipes.py $(REPO)/tools/music $(HARNESS) "`$(B)/fms_verify --engine`" $(B)/recipes
	touch $@

# One record from each family the harness plays, the 16 regression goldens (pd, vector,
# formant) and the 16 sync, wavetable and Karplus-Strong goldens (), each at both
# rates, plus gain, dither, pingpong and compressor/duck: 64; a music pool takes session.tl first.
G       := $(REPO)/tests/golden
M       := $(REPO)/music
VSET    := $(G)/48000/sine.tl $(G)/44100/bell.tl $(G)/48000/route4.tl $(G)/space/pan.tl \
           $(G)/space/move.tl $(G)/fx/chain.tl $(G)/fx/stereo.tl $(G)/space/amb3-yaw.tl \
           $(G)/space/binaural/binaural3.tl $(G)/space/panlaw.tl $(G)/48000/lowpass.tl \
           $(G)/48000/noise.tl $(G)/48000/fixedop.tl $(G)/44100/ratescale.tl \
           $(G)/gen/genpink-48000.tl $(G)/gen/genpink-44100.tl \
           $(G)/gen/gencoprime-48000.tl $(G)/gen/gencoprime-44100.tl \
           $(G)/gen/genlsystem-48000.tl $(G)/gen/genlsystem-44100.tl \
           $(G)/48000/pdknee.tl $(G)/44100/pdknee.tl $(G)/48000/pdmax.tl $(G)/44100/pdmax.tl \
           $(G)/48000/pdmin.tl $(G)/44100/pdmin.tl $(G)/48000/pdmorph.tl $(G)/44100/pdmorph.tl \
           $(G)/vector/vectorcorners-48000.tl $(G)/vector/vectorcorners-44100.tl \
           $(G)/48000/vectorxy.tl $(G)/44100/vectorxy.tl \
           $(G)/48000/formant3.tl $(G)/44100/formant3.tl \
           $(G)/48000/formantmove.tl $(G)/44100/formantmove.tl \
           $(G)/48000/syncturn.tl $(G)/44100/syncturn.tl $(G)/48000/syncback.tl $(G)/44100/syncback.tl \
           $(G)/48000/wtframe.tl $(G)/44100/wtframe.tl $(G)/48000/wtmorph.tl $(G)/44100/wtmorph.tl \
           $(G)/48000/wtbands.tl $(G)/44100/wtbands.tl $(G)/48000/additive.tl $(G)/44100/additive.tl \
           $(G)/48000/kspluck.tl $(G)/44100/kspluck.tl $(G)/48000/ksbend.tl $(G)/44100/ksbend.tl \
           $(G)/fx/gain.tl $(G)/fx/gain-44100.tl $(G)/fx/dither.tl $(G)/fx/dither-44100.tl \
           $(G)/fx/pingpong.tl $(G)/fx/pingpong-44100.tl \
           $(G)/fx/compressor.tl $(G)/fx/compressor-44100.tl \
           $(G)/fx/compressor-fast.tl $(G)/fx/compressor-fast-44100.tl \
           $(G)/fx/duck.tl $(G)/fx/duck-44100.tl \
           $(G)/fx/flanger.tl $(G)/fx/flanger-44100.tl \
           $(G)/fx/flanger-static.tl $(G)/fx/flanger-static-44100.tl \
           $(G)/fx/decodable.tl $(G)/fx/decodable-44100.tl \
           $(G)/fx/crush.tl $(G)/fx/crush-mono-44100.tl \
           $(G)/fx/crush-stereo.tl $(G)/fx/crush-44100.tl \
           $(G)/fx/field-amb1-44100.tl $(G)/fx/field-amb1-48000.tl \
           $(G)/fx/field-amb3-44100.tl $(G)/fx/field-amb3-48000.tl \
           $(G)/fx/field-bin1-44100.tl $(G)/fx/field-bin1-48000.tl \
           $(G)/fx/field-bin3-44100.tl $(G)/fx/field-bin3-48000.tl

$(B)/catalog_verify: $(HERE)/App/FeaturesView.swift $(HERE)/App/Params.swift $(HERE)/tools/verify_catalog.swift
	$(SWIFTC) $(SWIFT_DEFS) -O -target arm64-apple-macos14.0 -parse-as-library $^ -framework SwiftUI -o $@

# Check the shipped resource without rebuilding or repairing it. Keep harness
# failure separate from the comparison so a successful cmp cannot hide it.
catalog-bundle: $(B)/lib.stamp
	@$(HARNESS) --gen-params > $(B)/gen-params-current.txt
	@if ! cmp -s $(B)/gen-params-current.txt $(APP)/Contents/Resources/gen-params.txt; then echo 'CATALOG_BUNDLE mismatch'; exit 1; fi
	@echo 'CATALOG_BUNDLE ok'

params-bundle: $(B)/lib.stamp
	@$(HARNESS) --params > $(B)/params-current.txt
	@if ! cmp -s $(B)/params-current.txt $(APP)/Contents/Resources/params.txt; then echo 'PARAMS_BUNDLE mismatch'; exit 1; fi
	@echo 'PARAMS_BUNDLE ok'

verify: $(APP)/Contents/MacOS/IntegerFM $(B)/fms_verify $(B)/fms_verify_m1 $(B)/fms_verify_m2 $(B)/fms_verify_m3 $(B)/fms_verify_m4 $(B)/lib.stamp $(B)/gen-params.txt $(B)/params.txt $(B)/catalog_verify $(B)/lines_model $(B)/files_model $(B)/library_model $(B)/local_store_model recipes
	@python3 -B $(HERE)/tests/abi_check.py $(HERE)/bridge $(LIB) $(B)/abi-check
	@python3 -B $(HERE)/tests/export_forms.py $(HERE)/tools/make_recipes.py $(REPO)/tools/music $(HARNESS) "`$(B)/fms_verify --engine`"
	@python3 -B $(HERE)/tests/export_forms_mutant.py $(HERE)/tools/make_recipes.py $(REPO)/tools/music $(HARNESS) "`$(B)/fms_verify --engine`"
	@$(B)/library_model $(B) $(REPO)
	@$(B)/local_store_model
	@python3 -I $(HERE)/tools/test_tlplay.py $(B)
	@$(B)/catalog_verify $(APP)/Contents/Resources/gen-params.txt $(APP)/Contents/Resources/params.txt
	@$(MAKE) --no-print-directory catalog-bundle params-bundle
	@set -eu; \
	    bundle='$(APP)/Contents/Resources/gen-params.txt'; \
	    saved=$$(mktemp '$(B)/gen-params-bundle.XXXXXX'); \
	    cp "$$bundle" "$$saved"; \
	    trap 'cp "$$saved" "$$bundle"; rm -f "$$saved"' EXIT; \
	    trap 'exit 1' HUP INT TERM; \
	    printf '\n# corrupted bundle control\n' >> "$$bundle"; \
	    if $(MAKE) --no-print-directory catalog-bundle > '$(B)/catalog-bundle-negative.log' 2>&1; then \
	        echo 'CATALOG_BUNDLE control FAIL: corrupted bundle passed'; exit 1; \
	    fi; \
	    grep -q '^CATALOG_BUNDLE mismatch$$' '$(B)/catalog-bundle-negative.log'; \
	    cp "$$saved" "$$bundle"; \
	    $(MAKE) --no-print-directory catalog-bundle; \
	    echo 'CATALOG_BUNDLE control ok: corrupted shipped resource refused; original restored'
	@set -eu; \
	    bundle='$(APP)/Contents/Resources/params.txt'; \
	    saved=$$(mktemp '$(B)/params-bundle.XXXXXX'); \
	    cp "$$bundle" "$$saved"; \
	    trap 'cp "$$saved" "$$bundle"; rm -f "$$saved"' EXIT; \
	    trap 'exit 1' HUP INT TERM; \
	    $(B)/catalog_verify --mutate-param-range "$$saved" > "$$bundle"; \
	    if $(MAKE) --no-print-directory params-bundle > '$(B)/params-bundle-negative.log' 2>&1; then \
	        echo 'PARAMS_BUNDLE control FAIL: changed range passed'; exit 1; \
	    fi; \
	    grep -q '^PARAMS_BUNDLE mismatch$$' '$(B)/params-bundle-negative.log'; \
	    cp "$$saved" "$$bundle"; \
	    $(MAKE) --no-print-directory params-bundle; \
	    echo 'PARAMS_BUNDLE control ok: changed range refused; original restored'
	@sh $(HERE)/tools/verify.sh $(B)/fms_verify $(HARNESS) $(B) $(M) $(VSET)

# : the UI action suite, offline in script mode (tools/uitest.sh)
uitest: all
	@sh $(HERE)/tools/uitest.sh $(APP)/Contents/MacOS/IntegerFM $(HARNESS) $(HERE)/tools/uitest.txt $(B)/uitest $(REPO)/tools/music/analyze.py

run: all
	open $(APP)

clean:
	rm -rf $(B)

.PHONY: all standalone standalone-check native recipes verify catalog-bundle params-bundle uitest run clean
