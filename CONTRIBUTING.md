# Contributing to Euler

Explain the observed behavior, expected behavior, and a minimal reproduction. Use synthetic examples and remove credentials and private research records.

Install with `python -m pip install -e '.[dev]'` and run `python -m pytest euler/ -q`. Subsystem tests live beside their modules. Preserve type annotations, license headers, and the surrounding code style.

Pull requests should identify affected modules and validation commands. Include screenshots for UI changes. Keep changes focused; subjects such as `fix: ...`, `docs: ...`, and `feat: ...` match the project conventions.

Use the Fact-admission verification path for mathematical results. Record infrastructure errors as operational failures. Keep runtime state and account configuration in ignored local files, and preserve source attribution.

Submit Euler-original contributions under the terms in [LICENSING.md](LICENSING.md). Preserve all upstream and third-party license notices.
