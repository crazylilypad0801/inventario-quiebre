import json, math, os, sqlite3, threading
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(os.environ.get('DATA_DIR', HERE), 'data.db')
LOCK = threading.RLock()
STATE = {'forecast_up': True}

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now().strftime('%H:%M:%S')

def audit(c, actor, event, detail):
    c.execute('INSERT INTO audit(ts,actor,event,detail) VALUES(?,?,?,?)', (now(), actor, event, detail))

def init():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY, product TEXT, warehouse TEXT, qty REAL, forecast_rate REAL, avg7 REAL, UNIQUE(product,warehouse));
    CREATE TABLE IF NOT EXISTS recs(id INTEGER PRIMARY KEY, product TEXT, warehouse TEXT, cover REAL, risk TEXT, action TEXT, qty REAL, from_wh TEXT, source TEXT, status TEXT, created REAL, decided REAL);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, rec_id INTEGER UNIQUE, product TEXT, warehouse TEXT, qty REAL, created TEXT);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, event TEXT, detail TEXT);''')
    if not c.execute('SELECT 1 FROM stock').fetchone():
        seed = {
            'Pollo (kg)': [('Norte', 20, 18, 16), ('Centro', 260, 20, 18), ('Sur', 90, 15, 14)],
            'Arroz (kg)': [('Norte', 200, 25, 24), ('Centro', 40, 22, 20), ('Sur', 400, 20, 19)],
            'Aceite (L)': [('Norte', 30, 10, 9), ('Centro', 28, 9, 9), ('Sur', 120, 8, 8)],
            'Queso (kg)': [('Norte', 12, 6, 6), ('Centro', 60, 7, 6), ('Sur', 14, 5, 5)],
        }
        for p, rows in seed.items():
            for w, q, f, a in rows:
                c.execute('INSERT INTO stock(product,warehouse,qty,forecast_rate,avg7) VALUES(?,?,?,?,?)', (p, w, q, f, a))
        c.commit()
    c.close()

def rate(row):
    return row['forecast_rate'] if STATE['forecast_up'] else row['avg7']

def source():
    return 'pronóstico' if STATE['forecast_up'] else 'promedio 7d (respaldo)'

def generate():
    with LOCK:
        c = db()
        c.execute("DELETE FROM recs WHERE status='PENDIENTE'")
        rows = c.execute('SELECT * FROM stock').fetchall()
        n = 0
        for r in rows:
            cover = r['qty'] / rate(r)
            if cover >= 5:
                continue
            need = math.ceil(rate(r) * 5 - r['qty'])
            best = None
            for d in rows:
                if d['product'] == r['product'] and d['id'] != r['id']:
                    surplus = d['qty'] - rate(d) * 7
                    if surplus >= need and (best is None or surplus > best[1]):
                        best = (d['warehouse'], surplus)
            c.execute('INSERT INTO recs(product,warehouse,cover,risk,action,qty,from_wh,source,status,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (r['product'], r['warehouse'], round(cover, 1), 'ALTO' if cover < 3 else 'MEDIO',
                       'TRANSFERIR' if best else 'COMPRAR', need, best[0] if best else None, source(), 'PENDIENTE', datetime.now().timestamp()))
            n += 1
        audit(c, 'sistema', 'RECOMENDACIONES_GENERADAS', f'{n} recomendaciones, fuente={source()}')
        c.commit()
        c.close()
        return n

def decide(rid, ok, actor):
    with LOCK:
        c = db()
        r = c.execute('SELECT * FROM recs WHERE id=?', (rid,)).fetchone()
        if not r:
            return 404, {'error': 'no existe'}
        if r['status'] != 'PENDIENTE':
            return 409, {'error': 'ya fue resuelta: ' + r['status']}
        if not ok:
            c.execute("UPDATE recs SET status='RECHAZADA',decided=? WHERE id=?", (datetime.now().timestamp(), rid))
            audit(c, actor, 'RECHAZADA', f"#{rid} {r['product']} @{r['warehouse']}")
            c.commit()
            return 200, {'status': 'RECHAZADA'}
        if r['action'] == 'TRANSFERIR':
            d = c.execute('SELECT * FROM stock WHERE product=? AND warehouse=?', (r['product'], r['from_wh'])).fetchone()
            if d['qty'] < r['qty']:
                return 409, {'error': 'la bodega origen ya no tiene stock suficiente, regenera recomendaciones'}
            c.execute('UPDATE stock SET qty=qty-? WHERE product=? AND warehouse=?', (r['qty'], r['product'], r['from_wh']))
            c.execute('UPDATE stock SET qty=qty+? WHERE product=? AND warehouse=?', (r['qty'], r['product'], r['warehouse']))
            detail = f"transferencia {r['qty']} {r['product']} {r['from_wh']}→{r['warehouse']}"
        else:
            # integración con compras: la orden es única por recomendación (idempotente)
            c.execute('INSERT OR IGNORE INTO orders(rec_id,product,warehouse,qty,created) VALUES(?,?,?,?,?)',
                      (rid, r['product'], r['warehouse'], r['qty'], now()))
            detail = f"orden de compra {r['qty']} {r['product']} para {r['warehouse']}"
        c.execute("UPDATE recs SET status='APROBADA',decided=? WHERE id=?", (datetime.now().timestamp(), rid))
        audit(c, actor, 'APROBADA', f'#{rid} {detail} fuente={r["source"]}')
        c.commit()
        return 200, {'status': 'APROBADA', 'detail': detail}

def rows(sql, args=()):
    c = db()
    out = [dict(x) for x in c.execute(sql, args).fetchall()]
    c.close()
    return out

def route(method, path, body):
    p = [x for x in path.split('/') if x][1:]
    if method == 'GET' and p == ['health']:
        return 200, {'ok': True}
    if method == 'GET' and p == ['stock']:
        out = rows('SELECT * FROM stock ORDER BY product,warehouse')
        for r in out:
            r['rate'] = rate(r)
            r['cover'] = round(r['qty'] / rate(r), 1)
        return 200, {'forecast_up': STATE['forecast_up'], 'source': source(), 'stock': out}
    if method == 'POST' and p == ['forecast']:
        STATE['forecast_up'] = bool(body.get('up'))
        c = db(); audit(c, 'sistema', 'PRONOSTICO', 'disponible' if STATE['forecast_up'] else 'NO disponible → respaldo promedio 7d'); c.commit(); c.close()
        return 200, {'forecast_up': STATE['forecast_up']}
    if method == 'POST' and p == ['stock', 'set']:
        c = db(); c.execute('UPDATE stock SET qty=? WHERE id=?', (float(body['qty']), int(body['id']))); audit(c, 'encargado', 'AJUSTE_STOCK', f"id {body['id']} → {body['qty']}"); c.commit(); c.close()
        return 200, {'ok': True}
    if method == 'POST' and p == ['recs', 'generate']:
        return 200, {'generadas': generate()}
    if method == 'GET' and p == ['recs']:
        return 200, rows('SELECT * FROM recs ORDER BY id')
    if method == 'POST' and len(p) == 3 and p[0] == 'recs' and p[2] in ('approve', 'reject'):
        return decide(int(p[1]), p[2] == 'approve', 'encargado')
    if method == 'GET' and p == ['orders']:
        return 200, rows('SELECT * FROM orders ORDER BY id DESC')
    if method == 'GET' and p == ['audit']:
        return 200, rows('SELECT * FROM audit ORDER BY id DESC LIMIT 60')
    if method == 'GET' and p == ['metrics']:
        r = rows('SELECT * FROM recs')
        dec = [x for x in r if x['status'] != 'PENDIENTE']
        ap = [x for x in r if x['status'] == 'APROBADA']
        return 200, {
            'en_riesgo': len([x for x in r if x['status'] == 'PENDIENTE']),
            'aprobadas': len(ap),
            'aceptacion_pct': round(100 * len(ap) / len(dec)) if dec else None,
            'tiempo_aprobacion_s': round(sum(x['decided'] - x['created'] for x in dec) / len(dec), 1) if dec else None,
            'compras_urgentes': len([x for x in ap if x['action'] == 'COMPRAR']),
            'transferencias': len([x for x in ap if x['action'] == 'TRANSFERIR']),
        }
    return 404, {'error': 'ruta no encontrada'}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def reply(self, code, obj, ctype='application/json'):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def handle_any(self, method):
        if method == 'GET' and self.path in ('/', '/index.html'):
            return self.reply(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html')
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        try:
            code, obj = route(method, self.path.split('?')[0], body)
        except Exception as e:
            code, obj = 500, {'error': str(e)}
        self.reply(code, obj)
    def do_GET(self): self.handle_any('GET')
    def do_POST(self): self.handle_any('POST')

if __name__ == '__main__':
    init()
    print('Servidor en http://localhost:8000', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8000), H).serve_forever()
