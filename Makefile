.PHONY: doctor build up up-cm up-agent demo verify down down-cm down-agent reset
doctor build up up-cm up-agent demo down down-cm down-agent reset:
	python3 scripts/lab.py $@
verify:
	python3 tests/run.py
