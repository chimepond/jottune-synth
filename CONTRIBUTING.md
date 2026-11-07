# Code contributions

Describe the problem and intended behavior, and include the relevant checks in
any pull request. Keep synthesis integer-only and preserve existing TL output;
changing established audio or public format behavior requires explicit review.
Do not regenerate or replace golden records to hide a regression.

Use `make quick` for an initial native check, `make check` for the regression
suite, and the relevant browser/native bridge tests documented in docs/BUILD.md.
Optional dependencies and targets not exercised must be reported as untested.
New samples must be original material with redistribution rights. Song-only
contributions belong in the separate chimepond/tunes repository.

Security vulnerabilities and rights concerns should be reported privately using
SECURITY.md rather than a public issue containing sensitive details.
