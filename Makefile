# Sourced Intel - developer shortcuts
#
#   make demo       render the bundled sample report (no network, no API key)
#   make preflight  verify the whole chain WITHOUT installing anything
#   make rehearse   run the FULL live chain (fetch -> parse -> GLM) with zero packages
#   make venv       create the virtualenv and install dependencies
#   make check      verify every data source is reachable (run this first)
#   make test       dependency-free unit tests
#   make test-offline  everything that needs no network and no credential
#   make test-all   full suite including the live end-to-end run
#   make lint       ruff check + ruff format --check (install with: pip install ruff)
#   make lock       regenerate requirements.lock (needs uv)
#   make ui         start the Streamlit demo
#   make api        start the FastAPI service
#   make docker     build and start everything with docker compose
#   make clean      remove run artefacts and caches

PYTHON ?= python3
VENV   ?= .venv
BIN     = $(VENV)/bin
UV     ?= uv

.PHONY: help demo preflight rehearse venv check test test-offline test-all lint lock ui api docker docker-down clean tree

help:
	@grep -E '^#   ' Makefile | sed 's/^#   //'

# Renders examples/demo-report.html from bundled data. Routed through the same
# entry point a user runs, so a broken CLI fails here rather than in a
# newcomer's terminal - and through scripts/demo.py so it works without
# `pip install -e .`.
demo:
	$(PYTHON) scripts/demo.py --out examples/demo-report.html

preflight:
	$(PYTHON) scripts/preflight.py

# Runs the real fetch -> parse -> prompt -> GLM -> amount-cross-check path using
# only the standard library, so the chain can be proven before anything is
# installed. Add ARGS to replay a specific set of articles, e.g.
#   make rehearse ARGS="--urls https://www.china5e.com/news/news-1209422-1.html"
ARGS ?=
rehearse:
	$(PYTHON) scripts/rehearse_glm.py --docs 3 $(ARGS)

venv:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	@echo
	@echo "Done. Activate with:  source $(BIN)/activate"
	@echo "Then set GLM_API_KEY in .env and run:  make preflight"

check:
	$(PYTHON) scripts/check_sources.py

# The stdlib-only parsing tests: these must pass in a bare interpreter.
test:
	$(PYTHON) -m unittest discover -s tests -p "test_parsing.py" -v

# Everything that needs no network and no credential. This is what CI runs.
test-offline:
	$(BIN)/pytest tests/ -m "not slow" -q

test-all:
	$(BIN)/pytest tests/ -v

lint:
	ruff check .
	ruff format --check .

# Universal + hashes: one file works on every supported Python and OS. Compiled
# at the floor version (3.10) so the result cannot quietly require 3.11.
lock:
	$(UV) pip compile pyproject.toml --all-extras --universal --python-version 3.10 \
		--generate-hashes -o requirements.lock

ui:
	$(BIN)/streamlit run app/streamlit_app.py

api:
	$(BIN)/uvicorn sourced_intel.api:app --reload --app-dir src

docker:
	docker compose up --build

docker-down:
	docker compose down

clean:
	rm -rf data/runs data/cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache

tree:
	@find . -type f \
		-not -path './.git/*' -not -path './.venv/*' \
		-not -path './data/*' -not -name '*.pyc' \
		| sort
