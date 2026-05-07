"""Pytest config — añade el dir raíz del proyecto al sys.path."""
import os
import sys

PROYECTO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROYECTO not in sys.path:
    sys.path.insert(0, PROYECTO)
