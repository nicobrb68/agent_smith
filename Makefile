# Variables
PYTHON = uv run python
FLAKE8 = uv run flake8
MYPY = uv run mypy
TARGETS = student main.py mcp_tools_mbpp.py mcp_tools_swebench.py

.PHONY: all install lint lint-strict test-mbpp test-swebench clean dump-swebench dump-mbpp

all: install

install:
	@echo "Installing dependencies with uv..."
	uv sync

lint:
	@echo "Running mandatory Flake8 and Mypy checks..."
	$(FLAKE8) $(TARGETS)
	$(MYPY) --warn-return-any \
		--warn-unused-ignores \
		--ignore-missing-imports \
		--disallow-untyped-defs \
		--check-untyped-defs \
		$(TARGETS)


# Run a sample dump/eval cycle for MBPP based on subject CLI
test-mbpp:
	@echo "Running MBPP Agent environment check..."
	mkdir -p cache
	$(PYTHON) -m student.agent_mbpp --task-file cache/mbpp_task.json \
		--output cache/mbpp_solution.json \
		--model-name "llama-3.3-70b-versatile" \
        --provider-url "https://api.groq.com/openai/v1"

# Run a sample dump/eval cycle for SWE-bench based on subject CLI
test-swebench:
	@echo "Running SWE-bench Agent environment check..."
	mkdir -p cache
	$(PYTHON) -m student.agent_swebench --task-file cache/swebench_task.json \
		--output cache/swebench_solution.json \
		--model-name "llama-3.3-70b-versatile" \
        --provider-url "https://api.groq.com/openai/v1"

clean:
	@echo "Cleaning up caches and temporary files..."
	rm -rf .venv .mypy_cache .pytest_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +

dump-swebench:
	@echo "Generating swebench_task.json..."
	cd moulinette && uv run moulinette_eval dump swebench --output cache/swebench_task.json

dump-mbpp:
	@echo "Generating mbpp_task.json..."
	cd moulinette && uv run moulinette_eval dump mbpp --output cache/mbpp_task.json
	