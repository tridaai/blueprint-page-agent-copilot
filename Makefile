# Trida AI Blueprint — Page Agent Admin Copilot
#
# Primary workflow is uv (https://docs.astral.sh/uv/):
#   make install     uv sync --extra dev
#   make frontend    bundle the pinned page-agent package for the console
#   make demo        one gold task end to end, offline (no keys, no browser)
#   make test        pytest
#   make evals       scripted-task gate, offline driver
#   make serve       the OrderDesk console + API on http://localhost:8000
#
# No uv? python3 -m venv .venv && source .venv/bin/activate, then:
#   make install-pip

.PHONY: install install-pip frontend demo test evals serve

install:
	uv sync --extra dev

install-pip:
	# The frozen, hash-verified set first, then the package itself with
	# --no-deps: pip refuses an editable install under a hashed
	# constraints file, so the two steps never mix.
	pip install -r constraints.txt
	pip install --no-deps -e .
	pip install "pytest==9.1.1" "httpx==0.28.1"

frontend:
	cd frontend && npm ci && npm run build

demo:
	uv run page-agent-copilot-demo

test:
	uv run pytest -q

evals:
	uv run python evals/run_evals.py

serve:
	uv run uvicorn page_agent_copilot.api:app --port 8000
