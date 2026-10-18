.PHONY: seed load test eval api web

seed:
	python3 -m rationale.ingest seed --mode offline

load:
	python3 -m rationale.ingest load --reset

test:
	python3 -m pytest

eval:
	python3 -m eval --judge offline

api:
	uvicorn rationale.api.main:app --reload --host 0.0.0.0 --port 8000

web:
	npm --prefix web run dev

web-build:
	npm --prefix web ci && npm --prefix web run build
