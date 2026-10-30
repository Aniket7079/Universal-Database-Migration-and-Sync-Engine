"""
Test bootstrap — runs BEFORE any app module is imported.
Points the app's internal DB at a throwaway SQLite file so the whole
test suite runs with zero external services (no Postgres / Redis needed).
"""
import os

os.environ["DATABASE_URL"] = "sqlite:///./test_internal.db"
os.environ["DEBUG"] = "false"
