.PHONY: test demo-phase1 evaluate

test:
	uv run pytest

demo-phase1:
	uv run python tools/demo_phase1.py

evaluate:
	uv run python tools/demo_phase2.py
