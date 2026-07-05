import openqasm3
from openqasm3.ast import IntegerLiteral, QubitDeclaration


class ValidationError(Exception):
    pass


def validate_circuit(qc: str, max_qubits: int) -> int:
    """Parse `qc` as QASM3 and return its declared qubit count.

    Raises ValidationError on parse failure, zero declared qubits, or a
    count exceeding max_qubits (ADR-002). Structural validation only —
    gate semantics are checked later at the worker.
    """
    try:
        program = openqasm3.parse(qc)
    except Exception as exc:
        raise ValidationError(f"Unparseable QASM3: {exc}") from exc

    total_qubits = 0
    for statement in program.statements:
        if not isinstance(statement, QubitDeclaration):
            continue
        if statement.size is None:
            total_qubits += 1
        elif isinstance(statement.size, IntegerLiteral):
            total_qubits += statement.size.value
        else:
            raise ValidationError("Unsupported qubit declaration size expression")

    if total_qubits == 0:
        raise ValidationError("Circuit declares no qubits")
    if total_qubits > max_qubits:
        raise ValidationError(
            f"Circuit uses {total_qubits} qubits, exceeding MAX_QUBITS={max_qubits}"
        )

    return total_qubits
