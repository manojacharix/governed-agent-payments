.PHONY: test demo-phase1 evaluate mappings ledger simulate execute

test:
	uv run pytest

demo-phase1:
	uv run python tools/demo_phase1.py

evaluate:
	uv run python tools/demo_phase2.py

mappings:
	uv run python tools/demo_phase3.py

ledger:
	uv run python tools/demo_phase4.py

simulate:
	uv run python tools/simulate.py $(SEED) $(PAYERS) $(EVENTS)

execute:
	uv run python tools/demo_phase5.py
