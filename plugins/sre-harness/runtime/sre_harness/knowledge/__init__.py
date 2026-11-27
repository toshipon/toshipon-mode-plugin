"""The knowledge index: approved norms, filtered by scope and access, kept in one SQLite file.

The index is rebuilt from its sources and is never the source of truth. Only `store` speaks
SQL; everything else hands it plain records.
"""
