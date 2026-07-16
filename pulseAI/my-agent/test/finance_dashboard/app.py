"""Flask backend for the finance dashboard.

Provides three JSON endpoints:
- /api/balance          – returns the total balance (sum of account balances)
- /api/history          – returns the most recent transactions
- /api/bitcoin_price   – returns the current Bitcoin price in USD (fetched from CoinGecko)

Also serves the static HTML/CSS/JS files from the same directory.
"""

from flask import Flask, jsonify, send_from_directory
import sqlite3
import os
import requests

app = Flask(__name__, static_folder=".")

DB_PATH = os.path.join(os.path.dirname(__file__), "finance.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if not os.path.exists(DB_PATH):
        conn = get_db_connection()
        cur = conn.cursor()
        # Create a simple accounts table and a transactions table
        cur.execute(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                balance REAL NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
            """
        )
        # Insert sample data
        cur.execute("INSERT INTO accounts (name, balance) VALUES (?,?)", ("Checking", 1250.75))
        cur.execute("INSERT INTO accounts (name, balance) VALUES (?,?)", ("Savings", 5200.00))
        cur.execute("INSERT INTO accounts (name, balance) VALUES (?,?)", ("Credit Card", -300.40))
        conn.commit()
        # Sample transactions (some recent)
        cur.execute(
            "INSERT INTO transactions (account_id, date, description, amount) VALUES (1, '2024-07-01', 'Grocery Store', -45.23)"
        )
        cur.execute(
            "INSERT INTO transactions (account_id, date, description, amount) VALUES (2, '2024-06-30', 'Salary', 3000.00)"
        )
        cur.execute(
            "INSERT INTO transactions (account_id, date, description, amount) VALUES (3, '2024-07-02', 'Online Shopping', -120.00)"
        )
        conn.commit()
        conn.close()

# Ensure DB is initialized at import time
init_db()

@app.route('/')
def index():
    # Serve the main HTML file
    return send_from_directory(app.static_folder, "index.html")

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/api/balance')
def api_balance():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT SUM(balance) as total FROM accounts")
    row = cur.fetchone()
    total = row["total"] if row["total"] is not None else 0.0
    conn.close()
    return jsonify({"total_balance": total})

@app.route('/api/history')
def get_transaction_history():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT t.id, a.name as account, t.date, t.description, t.amount "
        "FROM transactions t JOIN accounts a ON t.account_id = a.id "
        "ORDER BY t.date DESC LIMIT 20"
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/bitcoin_price')
def api_bitcoin_price():
    # Fetch current Bitcoin price from CoinGecko
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=3, # Set timeout to 3 seconds
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get("bitcoin", {}).get("usd")
        if price is None:
            return jsonify({"error": "Price unavailable"}), 500
        return jsonify({"bitcoin_usd": price})
    except requests.exceptions.Timeout:
        return jsonify({"error": "Price unavailable (timeout)"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Price unavailable ({str(e)})"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Run Flask development server
    app.run(host='127.0.0.1', port=5000, debug=True)
