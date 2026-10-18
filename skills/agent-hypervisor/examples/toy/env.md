# Environment requirements

- toolchain: Python 3.11+, standard library only
- required `make` targets: `setup`, `build`, `test`, `lint`
- cold check image: `python:3.12-slim`
- acceptance: `make setup && make test` in a clean container, no network

`make test` discovers `tests/` and reports "no tests yet" before the first
increment lands, so the environment gate is meaningful from cycle 1.
