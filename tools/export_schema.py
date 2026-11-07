# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Reviewed native canonical rows used by external performance/sample exports.

These are conversion semantics, not a TL grammar. The native reader validates
input and supplies all fields. Keep one classification for both exporters;
new statement IDs require review even if the native reader already accepts them.
The descriptor coverage test detects additions that need this review.
"""

SCHEMA = {
    "header": frozenset("header_timeline header_engine header_fs header_length_ns "
                        "header_streams header_voice header_hash".split()),
    "patch": frozenset("patch route filter shape amp lfo peg fenv eg ks kstype fbtype "
                       "egloop noise pd vector formant ksource wtuse mod".split()),
    "asset": frozenset("wtbank wthash harmonic wtdata".split()),
    "space": frozenset("header_space header_block header_near header_order header_pinna "
                       "header_yaw header_panlaw".split()),
    "fx": frozenset("dcblock chorus flanger delay pingpong reverb compressor limiter "
                    "softclip gain dither crush decodable".split()),
    "event": frozenset("event_on event_pitch event_off event_pos event_move event_send "
                       "event_filter event_ctl event_yaw event_crush".split()),
}
