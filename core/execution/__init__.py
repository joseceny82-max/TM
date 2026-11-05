"""core/execution/ — execution layers (M21+).

Esta sub-rama del core está estrictamente DESACOPLADA de
`core/observability/` (M19, M20). Las capas de ejecución:
    - NO importan M19 (Domain Adapter)
    - NO importan M20 (Fidelity / Shadow)
    - NO contienen lógica de decisión (eso es M13–M18)
    - NO realizan análisis de mercado

Sólo traducen decisiones existentes en órdenes y resultados.
"""
