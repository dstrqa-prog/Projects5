import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
DB_NAME = "gigabiz_mb.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # الإعدادات الافتراضية للباقة الجديدة (800 GB = 819200 MB بسعر 6600 ليرة)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_master_mb REAL DEFAULT 819200.0,
            master_cost REAL DEFAULT 6600.0
        )
    ''')
    cursor.execute('INSERT OR IGNORE INTO settings (id, total_master_mb, master_cost) VALUES (1, 819200.0, 6600.0)')

    # جدول العملاء
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            remaining_mb REAL DEFAULT 0.0,
            total_billed REAL DEFAULT 0.0,
            total_paid REAL DEFAULT 0.0,
            created_at TEXT
        )
    ''')

    # جدول الحركات المالية والميجابايت
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            customer_name TEXT,
            tx_type TEXT, -- 'ADD_PKG', 'CONSUME', 'PAY_DEBT'
            mb_amount REAL DEFAULT 0.0,
            money_amount REAL DEFAULT 0.0,
            payment_mode TEXT, -- 'PREPAID', 'POSTPAID', 'NONE'
            note TEXT,
            created_at TEXT,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
    ''')
    conn.commit()
    conn.close()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data', methods=['GET'])
def get_all_data():
    conn = get_db()
    settings = dict(conn.execute('SELECT * FROM settings WHERE id = 1').fetchone())
    customers = [dict(row) for row in conn.execute('SELECT * FROM customers ORDER BY id DESC').fetchall()]
    transactions = [dict(row) for row in conn.execute('SELECT * FROM transactions ORDER BY id DESC LIMIT 100').fetchall()]
    
    # حساب إجمالي الميجابايت المباعة للعملاء
    total_sold_mb = conn.execute("SELECT COALESCE(SUM(mb_amount), 0) FROM transactions WHERE tx_type = 'ADD_PKG'").fetchone()[0]
    conn.close()
    
    return jsonify({
        'settings': settings,
        'customers': customers,
        'transactions': transactions,
        'total_sold_mb': total_sold_mb
    })

@app.route('/api/customers', methods=['POST'])
def add_customer():
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'الاسم مطلوب'}), 400
    
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute('INSERT INTO customers (name, remaining_mb, total_billed, total_paid, created_at) VALUES (?, 0, 0, 0, ?)', (name, now))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/customers/<int:cid>', methods=['DELETE'])
def delete_customer(cid):
    conn = get_db()
    conn.execute('DELETE FROM customers WHERE id = ?', (cid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/package', methods=['POST'])
def add_package():
    data = request.json
    cid = data.get('customer_id')
    mb = float(data.get('mb', 3072.0))
    price = float(data.get('price', 60.0))
    mode = data.get('mode') # 'PREPAID' or 'POSTPAID'
    
    conn = get_db()
    cust = conn.execute('SELECT * FROM customers WHERE id = ?', (cid,)).fetchone()
    if not cust:
        conn.close()
        return jsonify({'error': 'العميل غير موجود'}), 404

    paid_now = price if mode == 'PREPAID' else 0.0
    
    # ترحيل الرصيد: جمع الميجابايت الجديدة فوق الرصيد المتبقي القديم تلقائياً
    conn.execute('''
        UPDATE customers 
        SET remaining_mb = remaining_mb + ?,
            total_billed = total_billed + ?,
            total_paid = total_paid + ?
        WHERE id = ?
    ''', (mb, price, paid_now, cid))

    status_txt = "مسبق - مقبوض" if mode == "PREPAID" else "لاحق - دين"
    note = f"شراء باقة {int(mb)} MB بـ {int(price)} ليرة ({status_txt})"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute('''
        INSERT INTO transactions (customer_id, customer_name, tx_type, mb_amount, money_amount, payment_mode, note, created_at)
        VALUES (?, ?, 'ADD_PKG', ?, ?, ?, ?, ?)
    ''', (cid, cust['name'], mb, price, mode, note, now))

    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/consume', methods=['POST'])
def consume_mb():
    data = request.json
    cid = data.get('customer_id')
    used_mb = float(data.get('used_mb', 0))
    
    conn = get_db()
    cust = conn.execute('SELECT * FROM customers WHERE id = ?', (cid,)).fetchone()
    if not cust:
        conn.close()
        return jsonify({'error': 'العميل غير موجود'}), 404

    # خصم الاستهلاك من المتبقي (ولا يقل الرصيد عن الصفر أبداً)
    new_rem = max(0.0, round(cust['remaining_mb'] - used_mb, 2))
    conn.execute('UPDATE customers SET remaining_mb = ? WHERE id = ?', (new_rem, cid))

    note = f"استهلاك يومي: {int(used_mb)} MB (المتبقي المرحّل للغد: {int(new_rem)} MB)"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute('''
        INSERT INTO transactions (customer_id, customer_name, tx_type, mb_amount, money_amount, payment_mode, note, created_at)
        VALUES (?, ?, 'CONSUME', ?, 0, 'NONE', ?, ?)
    ''', (cid, cust['name'], used_mb, note, now))

    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/pay', methods=['POST'])
def pay_debt():
    data = request.json
    cid = data.get('customer_id')
    amount = float(data.get('amount', 0))
    
    conn = get_db()
    cust = conn.execute('SELECT * FROM customers WHERE id = ?', (cid,)).fetchone()
    if not cust:
        conn.close()
        return jsonify({'error': 'العميل غير موجود'}), 404

    conn.execute('UPDATE customers SET total_paid = total_paid + ? WHERE id = ?', (amount, cid))

    note = f"تسديد دفعة مالية بقيمة {int(amount)} ليرة"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute('''
        INSERT INTO transactions (customer_id, customer_name, tx_type, mb_amount, money_amount, payment_mode, note, created_at)
        VALUES (?, ?, 'PAY_DEBT', 0, ?, 'PREPAID', ?, ?)
    ''', (cid, cust['name'], amount, note, now))

    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.json
    total_mb = float(data.get('total_master_mb', 819200.0))
    cost = float(data.get('master_cost', 6600.0))
    conn = get_db()
    conn.execute('UPDATE settings SET total_master_mb = ?, master_cost = ? WHERE id = 1', (total_mb, cost))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

if __name__ == '__main__':
    init_db()
    print("Server running on: http://127.0.0.1:5000")
    app.run(debug=True, port=5000)