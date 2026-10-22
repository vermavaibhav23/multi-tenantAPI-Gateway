from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from gateway_project.db_router import PrimaryReplicaRouter


class PrimaryReplicaRoutingTests(SimpleTestCase):
    def setUp(self):
        self.router = PrimaryReplicaRouter()

    @override_settings(MYSQL_CLUSTER_ENABLED=False, USE_SQLITE=True)
    def test_sqlite_development_uses_default(self):
        self.assertEqual(self.router.db_for_read(None), 'default')
        self.assertEqual(self.router.db_for_write(None), 'default')

    @override_settings(MYSQL_CLUSTER_ENABLED=True, USE_SQLITE=False)
    def test_normal_reads_use_router_read_only_port_alias(self):
        self.assertEqual(self.router.db_for_read(None), 'replica')
        self.assertEqual(self.router.db_for_write(None), 'default')

    @override_settings(MYSQL_CLUSTER_ENABLED=True, USE_SQLITE=False)
    def test_locking_and_atomic_reads_stick_to_primary(self):
        with patch('gateway_project.db_router.connections') as connections:
            connections.__getitem__.return_value.in_atomic_block = True
            self.assertEqual(self.router.db_for_read(None), 'default')
            connections.__getitem__.return_value.in_atomic_block = False
            self.assertEqual(self.router.db_for_read(None, for_write=True), 'default')

    def test_schema_migrates_only_on_primary(self):
        self.assertTrue(self.router.allow_migrate('default', 'routes'))
        self.assertFalse(self.router.allow_migrate('replica', 'routes'))
