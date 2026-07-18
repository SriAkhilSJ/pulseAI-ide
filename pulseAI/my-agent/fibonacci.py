
def fibonacci(n):
    """
    Calculates the Fibonacci sequence up to n.
    """
    sequence = []
    a, b = 0, 1
    while a <= n:
        sequence.append(a)
        a, b = b, a + b
    return sequence

if __name__ == "__main__":
    print("Fibonacci sequence up to 0:", fibonacci(0))
    print("Fibonacci sequence up to 1:", fibonacci(1))
    print("Fibonacci sequence up to 10:", fibonacci(10))
    print("Fibonacci sequence up to 50:", fibonacci(50))
