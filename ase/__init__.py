"""Minimal stub of the ``ase`` package required for test collection.

Only the ``Atoms`` class is needed by ``tests/step4_ollama_synthesizer``. The
implementation provides the attributes accessed in the test fixtures but does
not perform any real scientific computation.
"""


class Atoms:  # pragma: no cover
    """Placeholder for ``ase.Atoms``.

    The real ASE library offers a rich API for atomic structures. For the unit
    tests we only need an object that can be instantiated without arguments.
    """

    def __init__(self, *args, **kwargs):  # noqa: D401
        pass
