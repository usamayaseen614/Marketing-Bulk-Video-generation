"""Background job queue for the bulk video generator.

Submitting work writes a row to a SQLite table and returns immediately; a
separate worker process picks jobs up and runs them one at a time. At ~7 jobs a
day this is deliberately simpler than Redis/Celery and has no daemon to babysit.
"""
