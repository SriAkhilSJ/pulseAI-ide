"""Tiny demo app that prints a receipt total."""
from utils import calculate_total


def print_receipt(price):
    total = calculate_total(price)
    print(f"Total: {total}")


if __name__ == "__main__":
    print_receipt(50)
