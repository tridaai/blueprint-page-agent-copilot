"""Trida AI Blueprint — Page Agent Admin Copilot.

A governed task shell around Alibaba's PageAgent (the in-page GUI
agent): an operator states a job, the shell drafts a field-level plan
from the records, a human confirms it, PageAgent (or the labelled
offline driver) executes it, and the shell verifies the resulting
record state field by field before anything is called done.
"""

__version__ = "0.1.0"
