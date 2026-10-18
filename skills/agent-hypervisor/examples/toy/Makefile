PY ?= python3

.PHONY: setup build test lint

setup:
	@$(PY) -c "import sys; print('env ok', sys.version.split()[0])"

build:
	@$(PY) -m compileall -q toy tests 2>/dev/null || true

test:
	@if [ -d tests ] && ls tests/test_*.py >/dev/null 2>&1; then $(PY) -m unittest discover -s tests -q; else echo "no tests yet"; fi

lint:
	@$(PY) -m compileall -q toy 2>/dev/null || true
