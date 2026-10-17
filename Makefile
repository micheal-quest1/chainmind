.PHONY: help install verify run test e2e inventory bench smoke mcp docker deploy clean

help:           ## show this help
	@grep -E '^[a-z0-9_-]+:.*?##' $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:        ## create the venv and install pinned dependencies
	uv venv --python 3.13 .venv 2>/dev/null || python3 -m venv .venv
	.venv/bin/python -m pip install -q --upgrade pip 2>/dev/null || true
	uv pip install --python .venv/bin/python -r requirements.txt 2>/dev/null \
	 || .venv/bin/python -m pip install -q -r requirements.txt
	@echo "done. next: cp .env.example .env, add your keys, then 'make verify'"

verify:         ## check which Google APIs your keys can actually reach
	.venv/bin/python scripts/verify_apis.py

run:            ## start the dashboard + API on http://localhost:8080
	.venv/bin/uvicorn chainmind.api:app --port 8080

test:           ## run the unit test suite
	.venv/bin/python -m pytest tests/ -q

e2e:            ## demo 1: orders, self-healing, feedback loop, copilot
	.venv/bin/python scripts/e2e.py

inventory:      ## demo 2: replenishment, transfers, goods-in, returns
	.venv/bin/python scripts/e2e_inventory.py

bench:          ## latency benchmark (the judged metric)
	.venv/bin/python scripts/bench.py

smoke:          ## check a running instance: make smoke URL=https://...
	./scripts/smoke.sh $(or $(URL),http://localhost:8080)

mcp:            ## run the MCP server over stdio
	.venv/bin/python -m chainmind.mcp_server

docker:         ## build and run the container locally
	docker build -t chainmind . && docker run --rm -p 8080:8080 --env-file .env chainmind

deploy:         ## build and deploy to Google Cloud Run
	./deploy.sh

clean:          ## remove the local database and caches
	rm -rf data/*.db data/*.db-wal data/*.db-shm .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
