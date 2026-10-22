"""Use the same verified form adapter with Safari or a cloud browser."""
from contextlib import contextmanager
from contextvars import ContextVar

evaluator=ContextVar('portal_evaluator',default=None)

@contextmanager
def use_evaluator(function):
    token=evaluator.set(function)
    try:yield
    finally:evaluator.reset(token)
