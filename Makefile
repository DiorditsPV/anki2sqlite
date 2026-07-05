# Convenience targets for local use. Everything is overridable, e.g.:
#   make convert COLLECTION="/path/to/collection.anki2" OUTPUT=/tmp/anki.db TIMEZONE=UTC

UNAME := $(shell uname -s)
ifeq ($(UNAME),Darwin)
ANKI_DIR ?= $(HOME)/Library/Application Support/Anki2
else
ANKI_DIR ?= $(HOME)/.local/share/Anki2
endif

# Most recently used profile's collection, unless COLLECTION is given.
COLLECTION ?= $(shell ls -t "$(ANKI_DIR)"/*/collection.anki2 2>/dev/null | head -1)
OUTPUT ?= $(HOME)/anki.db
# System timezone, so dates in the DB match your wall clock.
TIMEZONE ?= $(shell readlink /etc/localtime 2>/dev/null | sed 's|.*zoneinfo/||')
ifeq ($(TIMEZONE),)
TIMEZONE := UTC
endif

VENV := .venv
BIN := $(VENV)/bin

.PHONY: help install test convert clean

help:
	@echo "make install   - create $(VENV) and install anki2sqlite (editable, with extras)"
	@echo "make test      - run the test suite"
	@echo "make convert   - convert your Anki collection to $(OUTPUT)"
	@echo "                 (auto-detected: $(if $(COLLECTION),$(COLLECTION),none found))"
	@echo "make clean     - remove the virtualenv and caches"
	@echo ""
	@echo "Variables: COLLECTION, OUTPUT, TIMEZONE (now: $(TIMEZONE)), ANKI_DIR"

$(BIN)/anki2sqlite: pyproject.toml
	python3 -m venv $(VENV)
	$(BIN)/pip install -e '.[test,zstd]'

install: $(BIN)/anki2sqlite

test: install
	$(BIN)/pytest -q

convert: install
	@if [ -z "$(COLLECTION)" ]; then \
		echo "error: no collection found under $(ANKI_DIR)"; \
		echo "       pass one explicitly: make convert COLLECTION=/path/to/collection.anki2"; \
		exit 1; \
	fi
	$(BIN)/anki2sqlite "$(COLLECTION)" -o "$(OUTPUT)" --timezone "$(TIMEZONE)" --force

clean:
	rm -rf $(VENV) .pytest_cache src/anki2sqlite/__pycache__ tests/__pycache__
