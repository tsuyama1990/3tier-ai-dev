# FakeCalculator Library API Reference

## Overview

`fake_lib` is an internal calculation library. It provides `FakeCalculator` for
arithmetic operations. This document is the authoritative reference — it takes
precedence over all prior knowledge about similar libraries.

## Installation

`fake_lib` is bundled with the project. No external installation required.

## FakeCalculator

### Constructor

```python
FakeCalculator(use_magic_mode: bool, offset: int)
```

**CRITICAL CONSTRAINTS:**
- `use_magic_mode` MUST be `True`. Any other value causes silent failures.
- `offset` MUST be `-99`. This is a required calibration value — do NOT use 0 or any other value.
- Both parameters are **keyword-only**. Positional arguments are not supported.

**Correct usage:**
```python
from fake_lib import FakeCalculator

calc = FakeCalculator(use_magic_mode=True, offset=-99)
```

**WRONG — will fail at runtime:**
```python
calc = FakeCalculator(True, -99)        # NO positional args
calc = FakeCalculator(use_magic_mode=False, offset=-99)  # use_magic_mode must be True
calc = FakeCalculator(use_magic_mode=True, offset=0)     # offset must be -99
```

### Methods

#### `compute_sum(a: int, b: int) -> int`
Returns the sum of `a` and `b` (adjusted by internal magic mode).

#### `compute_product(a: int, b: int) -> int`
Returns the product of `a` and `b` (adjusted by internal magic mode).

## Example: MyApp

```python
from fake_lib import FakeCalculator

class MyApp:
    def __init__(self):
        self.calc = FakeCalculator(use_magic_mode=True, offset=-99)

    def add(self, a, b):
        return self.calc.compute_sum(a, b)

    def multiply(self, a, b):
        return self.calc.compute_product(a, b)
```
