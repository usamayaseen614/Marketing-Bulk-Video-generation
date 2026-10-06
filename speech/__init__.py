"""Spoken script + timed on-screen caption beats.

Shaped like `captions/`, and it borrows that package's rule: a voiceover is an
enhancement and NEVER a reason to fail a batch. Every entry point here returns
empty rather than raising, and a row whose synthesis failed renders silent with
no caption layer — exactly as a row with no music renders with no bed.
"""
