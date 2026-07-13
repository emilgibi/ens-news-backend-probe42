.PHONY: lint format

# Run all linting tools
lint:
	flake8 .
	pylint your_project_name
	black --check .
	isort --check-only .

# Auto-format the code
format:
	black .
	isort .
