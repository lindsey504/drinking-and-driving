#!/bin/bash
set -e

# Find python and pip
PYTHON=$(which python3.11 2>/dev/null || which python3 2>/dev/null || which python 2>/dev/null)
PIP="$PYTHON -m pip"

echo "Using Python: $PYTHON"

# Install dependencies
$PIP install -r requirements.txt -q

# Run with gunicorn via python -m (avoids PATH issues)
exec $PYTHON -m gunicorn app:app --bind 0.0.0.0:8080 --workers 1 --timeout 120
