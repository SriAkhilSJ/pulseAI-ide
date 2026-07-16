// Sample file for AST transform testing
var globalCounter = 0;
const config = { debug: true };
const apiKey = "secret";

function calculateTotal(amount, tax) {
    const subtotal = amount;
    var total = subtotal + tax;
    total = total * 1.05;
    return total;
}

function formatCurrency(value) {
    return "$" + value.toFixed(2);
}

globalCounter = 1;
