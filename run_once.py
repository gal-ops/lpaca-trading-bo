"""Single-cycle entry point used by GitHub Actions."""
from main import test_connection, run_bot

test_connection()
run_bot()
