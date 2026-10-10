.PHONY: install
install:
	pip install $(if $(filter dev,$(MODE)),-e) .

.PHONY: test
test:
	python -m pytest tests
