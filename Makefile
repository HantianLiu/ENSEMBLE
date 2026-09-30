PYTHON ?= .venv/bin/python

.PHONY: test smoke schemas

test:
	$(PYTHON) -m pytest

smoke:
	PYTHON_BIN=$(PYTHON) bash scripts/smoke_test.sh

schemas:
	$(PYTHON) scripts/export_json_schemas.py
