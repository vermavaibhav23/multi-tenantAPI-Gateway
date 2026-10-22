"""Conservative Django read/write routing for MySQL InnoDB Cluster.

Reads outside write transactions use MySQL Router's read-only port. Every write,
locking read, and read within an atomic(default) block uses the primary.
This relies on the DB cluster's BEFORE_AND_AFTER consistency configuration.
"""

from django.conf import settings
from django.db import connections


class PrimaryReplicaRouter:
    def db_for_read(self, model, **hints):
        if not settings.MYSQL_CLUSTER_ENABLED or settings.USE_SQLITE:
            return 'default'
        # Prevent reading stale data / losing read-your-writes inside a write TX.
        if connections['default'].in_atomic_block or hints.get('for_write'):
            return 'default'
        return 'replica'

    def db_for_write(self, model, **hints):
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        return True  # Both aliases connect to the SAME logical replicated DB.

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == 'default'  # Schema updates replicate via Group Replication.
