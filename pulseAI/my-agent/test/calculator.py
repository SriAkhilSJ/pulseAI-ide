def calculate_total(amount, tax):
    subtotal = amount
    total = subtotal + tax
    return total * 1.05

def format_currency(value):
    return "$" + str(value)
