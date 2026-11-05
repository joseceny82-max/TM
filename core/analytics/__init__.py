"""core/analytics/ — analytics layers (M22+).

Esta sub-rama del core está estrictamente DESACOPLADA de:
    - core/observability/ (M19 / M20)
    - core/execution/    (M21)

Las capas de analytics:
    - SOLO miden
    - NUNCA deciden
    - NUNCA ejecutan
    - NUNCA importan capas previas (consumo de structures, no de tipos)
"""
