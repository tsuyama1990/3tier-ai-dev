def fibonacci(n: int) -> list[int]:
    """Returns the first n Fibonacci numbers as a list."""
    if n <= 0:
        return []
    if n == 1:
        return [0]

    fib_sequence = [0, 1]
    for _ in range(2, n):
        next_value = fib_sequence[-1] + fib_sequence[-2]
        fib_sequence.append(next_value)

    return fib_sequence

# Test the function
assert fibonacci(10) == [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]
