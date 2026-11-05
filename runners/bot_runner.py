"""runners/bot_runner.py — Entry point del bot de compra.

FASE 2: placeholder. Llamar a este runner lanza NotImplementedError
intencionalmente (el bot real sigue siendo `_compra_parallel.py`).

FASE 6 conectará al `bot.purchase_flow` real cuando los flows estén
extraídos. Hasta entonces, el bot se ejecuta como siempre:

    python tools/oneoff/_compra_parallel.py --slot 1 --event-url ... --engine morelogin
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.purchase_flow import run_purchase_flow

if __name__ == "__main__":
    run_purchase_flow()
