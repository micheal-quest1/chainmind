.PHONY: install verify run e2e bench mcp docker clean

install:        ## create venv and install pinned deps
	uv venv --python 3.13 .venv || python3 -m venv .venv
	.venv/bin/python -m pip install -q --upgrade pip
	.venv/bin/python -m pip install -q -r requirements.txt

verify:         ## prove which Google APIs this key can reach
	.venv/bin/python scripts/verify_apis.py

run:            ## start the API + dashboard on :8080
	.venv/bin/uvicorn chainmind.api:app --reload --port 8080

e2e:            ## full demo: 4 orders, feedback loop, reflection, copilot
	.venv/bin/python scripts/e2e.py

bench:          ## latency benchmark
	.venv/bin/python scripts/bench.py

mcp:            ## run the MCP server over stdio
	.venv/bin/python -m chainmind.mcp_server

docker:
	docker build -t chainmind . && docker run --rm -p 8080:8080 --env-file .env chainmind

clean:
	rm -rf data/*.db data/*.db-wal data/*.db-shm __pycache__ .pytest_cache
