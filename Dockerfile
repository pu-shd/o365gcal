# Test runner for the O365GCal reference engine and solution validation.
#
# The flows themselves cannot execute outside Power Automate, so this image runs the
# layers that can be verified offline: the reference engine's unit tests, the
# expression-parity checks against a WDL evaluator, the mocked Outlook/Google
# integration cycles, and the static validation of the packed solution.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY pytest.ini Makefile README.md ./
COPY src/ ./src/
COPY tests/ ./tests/
COPY solution/ ./solution/
COPY scripts/ ./scripts/
COPY docs/ ./docs/

# test_scripts.py parses the lifecycle scripts as zsh.
RUN apt-get update && apt-get install -y --no-install-recommends zsh \
    && rm -rf /var/lib/apt/lists/*

CMD ["python", "-m", "pytest"]
