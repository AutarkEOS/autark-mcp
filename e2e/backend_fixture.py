"""Run the real Flask backend against an isolated database with demo history.

Started by run.py with the backend's own interpreter (mock-servivce/backend/.venv).
Usage: backend_fixture.py --database NAME --port PORT [--mode test-payments|chain]

A new database gets the schema, every migration, the demo catalog and a little
history: six agents rating vendor-c-apple 5/5, six rating vendor-b-apple 2/5, and ten
weather API calls (nine succeeded, ~180 ms). An existing database is reused as is.
Prints READY when the server accepts requests.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2] / 'mock-servivce'

parser = argparse.ArgumentParser()
parser.add_argument('--database', required=True)
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--mode', choices=('test-payments', 'chain'), default='test-payments')
options = parser.parse_args()
if not options.database.startswith('autark_e2e_'):
    sys.exit('The database name must start with autark_e2e_ so it can never be the main database.')
# test-payments: the backend's AUTARK_DB_TESTS path. Balance, ledger and receipts are real;
# only the on-chain transfer is replaced by a mock signature. chain: real Devnet transfers.
if options.mode == 'chain':
    os.environ.pop('AUTARK_DB_TESTS', None)
else:
    os.environ['AUTARK_DB_TESTS'] = '1'

os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from backend import database  # noqa: E402
from databse.seed import load_seed, seed_sql  # noqa: E402

settings = database.settings['db']


def connect():
    return psycopg.connect(host=settings['host'], port=settings['port'], dbname=options.database, user=settings['user'],
                           password=os.environ['POSTGRES_PASSWORD'], row_factory=dict_row)


def purchase(connection, user_id, product_id):
    offer = connection.execute('SELECT * FROM products WHERE id=%s', (product_id,)).fetchone()
    purchase_id = uuid4()
    connection.execute('INSERT INTO purchases(id,idempotency_key,request_fingerprint,total_minor,currency,user_id,status) '
                       "VALUES (%s,%s,%s,%s,'SOL',%s,'paid')",
                       (purchase_id, 'e2e-' + uuid4().hex, 'f' * 64, offer['price_minor'], user_id))
    connection.execute(
        'INSERT INTO purchase_items(purchase_id,product_id,quantity,unit_price_minor,vendor_id,kind,url,name,description,'
        'schema,response_schema,response_field) VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (purchase_id, product_id, offer['price_minor'], offer['vendor_id'], offer['kind'], offer['url'], offer['name'],
         offer['description'], json.dumps(offer['schema']) if offer['schema'] is not None else None,
         json.dumps(offer['response_schema']) if offer['response_schema'] is not None else None, offer['response_field']))
    return purchase_id


def create_database():
    admin = database.connect_db()
    admin.autocommit = True
    admin.execute('SET statement_timeout = 0')
    exists = admin.execute('SELECT 1 FROM pg_database WHERE datname=%s', (options.database,)).fetchone()
    if not exists:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(options.database)))
    admin.close()
    if exists:
        return
    with connect() as connection:
        connection.autocommit = True
        connection.execute((ROOT / 'databse/init/001-schema.sql').read_text(encoding='utf-8'))
        for file in sorted((ROOT / 'databse/migrations').glob('*.sql')):
            connection.execute(file.read_text(encoding='utf-8'))
        connection.execute(seed_sql(load_seed('databse/seeds/products.json')))
        for index in range(6):
            for product_id, rating in (('vendor-c-apple', 5), ('vendor-b-apple', 2)):
                user = uuid4()
                connection.execute("INSERT INTO users(id,username,password_hash,role) VALUES (%s,%s,'unused','agent')",
                                   (user, f'e2e-{product_id}-{index}'))
                connection.execute('UPDATE purchases SET evaluation_rating=%s WHERE id=%s',
                                   (rating, purchase(connection, user, product_id)))
        caller = uuid4()
        connection.execute("INSERT INTO users(id,username,password_hash,role) VALUES (%s,'e2e-caller','unused','agent')", (caller,))
        for index in range(10):
            receipt = purchase(connection, caller, 'vendor-a-thailand-weather')
            entitlement = connection.execute(
                "INSERT INTO service_entitlements(purchase_id,product_id,unit_number,status,used_at) "
                "VALUES (%s,'vendor-a-thailand-weather',1,'used',now()) RETURNING id", (receipt,)).fetchone()['id']
            connection.execute(
                "INSERT INTO api_calls(entitlement_id,arguments,status,started_at,completed_at) VALUES "
                "(%s,'{}',%s,now()-interval '1 minute',now()-interval '1 minute'+make_interval(secs => %s))",
                (entitlement, 'failed' if index == 0 else 'succeeded', (170 + index * 2) / 1000))


create_database()
patch.object(database, 'connect_db', connect).start()
from backend.app import app  # noqa: E402
from waitress import serve  # noqa: E402

print('READY', flush=True)
serve(app, host='127.0.0.1', port=options.port, threads=4)
