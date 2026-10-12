.PHONY: test demo-phase1

test:
	uv run pytest

demo-phase1:
	uv run python tools/demo_phase1.py
