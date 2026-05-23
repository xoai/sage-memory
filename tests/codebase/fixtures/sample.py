"""Fixture for T5 — Python extractor."""
import os
from collections import deque

def helper(x):
    return x + 1

class Greeter:
    def greet(self):
        return helper(self)

def outer():
    def inner_helper():
        return 1

def outer():  # type: ignore[no-redef]
    def inner_helper():
        return 2

helper(1); helper(2)
