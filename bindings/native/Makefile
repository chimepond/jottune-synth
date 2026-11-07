# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
# The host tools run locally; only bridge and engine objects use target flags.
ROOT := $(abspath $(dir $(abspath $(lastword $(MAKEFILE_LIST))))/../..)
HERE := $(ROOT)/bindings/native
HOSTCC ?= cc
HOSTCFLAGS ?= -O2
CC ?= cc
AR ?= ar
CFLAGS ?= -O2
CPPFLAGS ?=
LDFLAGS ?=
LDLIBS ?= -lm
SDK ?=
ARCH ?= arm64
IOS_MIN ?= 15.0

ifneq ($(SDK),)
ifneq ($(filter $(SDK),iphoneos iphonesimulator),$(SDK))
$(error SDK must be iphoneos or iphonesimulator)
endif
ifeq ($(origin CC),default)
CC := xcrun --sdk $(SDK) clang
endif
SDKROOT := $(shell xcrun --sdk $(SDK) --show-sdk-path)
ifeq ($(SDKROOT),)
$(error Cannot locate SDK $(SDK))
endif
IOS_TARGET := $(ARCH)-apple-ios$(IOS_MIN)$(if $(filter iphonesimulator,$(SDK)),-simulator)
TARGET_FLAGS ?= -target $(IOS_TARGET) -isysroot $(SDKROOT)
BUILD_DIR ?= $(ROOT)/build/native-$(SDK)-$(ARCH)
else
TARGET_FLAGS ?=
BUILD_DIR ?= $(ROOT)/build/native
endif

# Override for a different linker, e.g. STRIP_FLAGS=-Wl,--gc-sections on Android.
ifeq ($(shell uname -s),Darwin)
STRIP_FLAGS ?= -Wl,-dead_strip
else
STRIP_FLAGS ?= -Wl,--gc-sections
endif

B := $(abspath $(BUILD_DIR))
WARN := -std=c99 -Wall -Wextra -Wshadow -ffp-contract=off
DEFS := -DFM_SINE_BITS=12 -DFM_EXP_BITS=10 -DFM_PHASE64=1 -DFM_ENV64=1
INCLUDES := -I$(HERE) -I$(ROOT)/src/host -I$(B) -I$(ROOT)
HDEFS := -DSP_BINAURAL_H='"$(B)/sp_binaural_tables.h"'
TARGET_COMPILE = $(CC) $(WARN) $(CFLAGS) $(CPPFLAGS) $(TARGET_FLAGS) $(DEFS) $(INCLUDES) -fPIC -ffunction-sections -fdata-sections
OBJS := $(addprefix $(B)/,jottune_bridge.o fms_midi.o fm_a1.o gen.o space.o fx.o field.o ref_double.o fx_ref.o field_ref.o)
ENGINE_HEADERS := $(wildcard $(ROOT)/src/*/*.h $(ROOT)/src/*/*.def $(ROOT)/vendor/*.h) $(HERE)/Makefile

all: $(B)/libjottune_native.a

libjottune_native.a: $(B)/libjottune_native.a

$(B) $(B)/host:
	mkdir -p $@

$(B)/host/gen_tables_int: $(ROOT)/src/host/gen_tables_int.c $(ROOT)/vendor/fp_math.h | $(B)/host
	$(HOSTCC) $(WARN) $(HOSTCFLAGS) -o $@ $<

$(B)/host/gen_binaural_int: $(ROOT)/src/space/gen_binaural_int.c $(ROOT)/vendor/fp_math.h | $(B)/host
	$(HOSTCC) $(WARN) $(HOSTCFLAGS) -o $@ $<

$(B)/fm_tables.h: $(B)/host/gen_tables_int | $(B)
	$< 12 10 > $@.tmp && mv $@.tmp $@

$(B)/sp_binaural_tables.h: $(B)/host/gen_binaural_int | $(B)
	$< > $@.tmp && mv $@.tmp $@

tables: $(B)/fm_tables.h $(B)/sp_binaural_tables.h

$(B)/jottune_bridge.o: $(HERE)/jottune_bridge.c $(HERE)/jottune_bridge.h $(ROOT)/src/host/harness.c $(ENGINE_HEADERS) $(B)/fm_tables.h $(B)/sp_binaural_tables.h
	$(TARGET_COMPILE) $(HDEFS) -c -o $@ $<

$(B)/fms_midi.o: $(HERE)/fms_midi.c $(HERE)/jottune_bridge.h $(ENGINE_HEADERS)
	$(TARGET_COMPILE) -c -o $@ $<

# The included host CLI references these comparison models. Keeping them in the
# archive makes ordinary linking complete; application linkers can discard them.
$(B)/ref_double.o: $(ROOT)/src/host/ref_double.c $(ENGINE_HEADERS) $(B)/fm_tables.h
	$(TARGET_COMPILE) -c -o $@ $<
$(B)/fx_ref.o: $(ROOT)/src/host/fx_ref.c $(ENGINE_HEADERS)
	$(TARGET_COMPILE) -c -o $@ $<
$(B)/field_ref.o: $(ROOT)/src/host/field_ref.c $(ENGINE_HEADERS) $(B)/sp_binaural_tables.h
	$(TARGET_COMPILE) $(HDEFS) -c -o $@ $<

$(B)/fm_a1.o: $(ROOT)/src/host/fm_a1.c $(ENGINE_HEADERS) $(B)/fm_tables.h
	$(TARGET_COMPILE) -Werror -Wsign-conversion -c -o $@ $<

$(B)/gen.o: $(ROOT)/src/gen/gen.c $(ENGINE_HEADERS) | $(B)
	$(TARGET_COMPILE) -Werror -Wsign-conversion -c -o $@ $<

$(B)/space.o: $(ROOT)/src/space/space.c $(ENGINE_HEADERS) | $(B)
	$(TARGET_COMPILE) -Werror -Wsign-conversion -c -o $@ $<

$(B)/fx.o: $(ROOT)/src/fx/fx.c $(ENGINE_HEADERS) | $(B)
	$(TARGET_COMPILE) -Werror -Wsign-conversion -c -o $@ $<

$(B)/field.o: $(ROOT)/src/space/field.c $(ENGINE_HEADERS) | $(B)
	$(TARGET_COMPILE) -Werror -Wsign-conversion -c -o $@ $<

$(B)/libjottune_native.a: $(OBJS)
	$(AR) rcs $@ $^

# Forces the record parser and renderer into an executable, catching unresolved
# host-reference symbols that merely creating an archive cannot detect.
$(B)/link-smoke: $(B)/libjottune_native.a $(HERE)/jottune_bridge.h $(HERE)/Makefile
	printf '%s\n' '#include "jottune_bridge.h"' \
	    'int main(int argc, char **argv) {' \
	    '    char err[FMS_ERR_LEN]; int16_t pcm[64 * FMS_MAX_CH]; fms_info_t info; fms_player_t *p;' \
	    '    fms_player_t *derived = 0; uint32_t written = 0; fms_cancel_t *cancel = fms_cancel_create();' \
	    '    fms_midi_config_t cfg = {1, 0, 65535, 100, 200, 1, 0, 707, 0}; fms_midi_t *m = fms_midi_create(&cfg);' \
	    '    if (!m) return 5; fms_midi_reset(m); fms_midi_destroy(m);' \
	    '    if (!cancel) return 1; fms_cancel_request(cancel);' \
	    '    if (fms_player_import("x", 1, cancel, &derived, err, sizeof err) != FMS_CANCELLED) return 2;' \
	    '    fms_cancel_close(cancel);' \
	    '    if (fms_player_keys(0, 60, 0, 0, 0, &derived, err, sizeof err) != FMS_INVALID || fms_player_record_text(0, 0, 0)) return 3;' \
	    '    if (fms_player_render_checked(0, pcm, 0, 0, &written, err, sizeof err) != FMS_INVALID) return 4;' \
	    '    fms_engine_line(err, sizeof err); if (argc < 2) return !err[0];' \
	    '    p = fms_player_open((const char *const *)(argv + 1), argc - 1, err, sizeof err); if (!p) return 1;' \
	    '    fms_player_info(p, &info); (void)fms_player_render(p, pcm, 64); fms_player_close(p); return !info.fs;' \
	    '}' | $(CC) $(WARN) $(CFLAGS) $(CPPFLAGS) $(TARGET_FLAGS) -I$(HERE) \
	    -x c - -x none $(B)/libjottune_native.a $(LDFLAGS) $(LDLIBS) -o $@

link-smoke: $(B)/link-smoke

# Apple dynamic-library link also retains every exported fms_* entry point,
# rather than only the methods used by the small executable above.
$(B)/libjottune_native.dylib: $(OBJS)
	$(CC) $(TARGET_FLAGS) -dynamiclib $^ -Wl,-dead_strip $(LDFLAGS) $(LDLIBS) -o $@

dylib-smoke: $(B)/libjottune_native.dylib

$(B)/record-test: $(HERE)/tests/record_test.c $(HERE)/jottune_bridge.h $(B)/libjottune_native.a $(HERE)/Makefile
	$(CC) $(WARN) $(CFLAGS) $(CPPFLAGS) $(TARGET_FLAGS) -I$(HERE) -pthread $< \
	    $(B)/libjottune_native.a $(STRIP_FLAGS) $(LDFLAGS) $(LDLIBS) -o $@

# Run only a host build; phone archives and link-smoke are compile/link checks.
check: $(B)/record-test
	$< $(addprefix $(ROOT)/tests/golden/,48000/sine.tl 44100/bell.tl space/pan.tl \
	    space/binaural/binaural3.tl gen/genpink-48000.tl gen/genlsystem-44100.tl \
	    fx/chain.tl fx/stereo.tl fx/field-amb1-48000.tl fx/field-amb3-44100.tl \
	    fx/field-bin1-48000.tl fx/field-bin3-44100.tl 48000/wtmorph.tl 44100/kspluck.tl)

.PHONY: all libjottune_native.a tables link-smoke dylib-smoke check
