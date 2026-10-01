"""Pure orchestrator decisions (OMP-417).

This package holds only deterministic decisions: the stage decision in
``stages`` reads a frozen facts snapshot and returns one step, and later slices
add the verifier and qualification gates. It performs no I/O, reads no clock,
and takes no model input.
"""
