def add_numbers(a, b):
    return a + b


def test_add():
    assert add_numbers(2, 3) == 5


# Example usage:
if __name__ == "__main__":
    result = add_numbers(3, 5)
    print(f"The sum is: {result}")
