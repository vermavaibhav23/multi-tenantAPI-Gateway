"""Read-only topology and counter snapshot, run inside the benchmark gateway."""
import json
import os
import django
import pymysql

django.setup()
from django.conf import settings
from django.db import connections
from gateway.redis_client import get_redis_client

if settings.SETTINGS_MODULE != 'benchmarks.settings':
    raise RuntimeError('Benchmark settings required')

result = {'database_connections': {}, 'mysql_nodes': {}, 'redis_nodes': {}}
for alias in connections:
    with connections[alias].cursor() as cursor:
        cursor.execute('SELECT @@hostname, @@read_only, DATABASE()')
        host, readonly, database = cursor.fetchone()
        result['database_connections'][alias] = {'host': host, 'read_only': readonly, 'database': database}
        assert database == 'gatescale_benchmark'
        if alias == 'replica':
            assert readonly == 1, 'Replica connection is not read-only'

for host in (['mysql-1', 'mysql-2', 'mysql-3'] if settings.MYSQL_CLUSTER_ENABLED else ['mysql-1']):
    conn = pymysql.connect(host=host, user='root', password=os.environ['MYSQL_ROOT_PASSWORD'])
    with conn.cursor() as cursor:
        cursor.execute("SHOW GLOBAL STATUS WHERE Variable_name IN "
                       "('Questions','Queries','Threads_running','Threads_connected','Com_select')")
        stats = dict(cursor.fetchall())
        if settings.MYSQL_CLUSTER_ENABLED:
            cursor.execute('SELECT MEMBER_HOST, MEMBER_STATE, MEMBER_ROLE '
                           'FROM performance_schema.replication_group_members')
            members = cursor.fetchall()
            assert len(members) == 3 and all(row[1] == 'ONLINE' for row in members)
            stats['members'] = members
        result['mysql_nodes'][host] = stats
    conn.close()

client = get_redis_client()
if settings.REDIS_CLUSTER_ENABLED:
    nodes = client.get_nodes()
    assert len(client.get_primaries()) == 3 and len(client.get_replicas()) == 3
    node_clients = [(node.name, node.redis_connection) for node in nodes]
else:
    node_clients = [('redis-single', client)]
for name, connection in node_clients:
    info = connection.info()
    result['redis_nodes'][name] = {key: info[key] for key in (
        'role', 'connected_clients', 'used_memory', 'total_commands_processed',
        'keyspace_hits', 'keyspace_misses', 'evicted_keys')}
print(json.dumps(result, indent=2))
