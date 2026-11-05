"""core/backtesting/ — strategy backtesting layers (M23+).

Esta sub-rama del core orquesta capas previas para evaluar estrategias
sobre datos históricos simulados.

Reglas:
    - El motor puro (backtest_engine) NO importa M19/M20/M21/M22:
      consume callables inyectados.
    - El wiring por defecto (default_pipeline) SÍ importa el public
      API de M19/M21/M22 para conveniencia del usuario.
    - NUNCA modifica ninguna capa previa.
    - NUNCA introduce randomness ni IO.
"""
