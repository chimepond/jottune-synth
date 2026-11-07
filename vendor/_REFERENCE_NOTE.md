<!-- Copyright (c) 2026 Nenad Micic -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Fixed-point arithmetic header

The vendored `fp_math.h` is an Apache-2.0 dependency supplying Q16.48
arithmetic and equivalent native and portable 128-bit backends without mutable
global state. Its copyright and upstream lineage must be preserved.

The implementation derives from `astro-nav-int/fp_math.h` at revision
`9db73509c7c06d4a7bcdcd706c77a5fde3e728c1` and supplies immutable tables,
checked wide arithmetic and explicit fixed-point boundary conversions.
Its native and portable backends must produce equivalent results. Keep the
copyright and licence notices with redistributed copies.
