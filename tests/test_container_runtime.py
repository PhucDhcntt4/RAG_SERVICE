import unittest

from psycopg.conninfo import conninfo_to_dict

from app.container_runtime import database_url_for_container


class ContainerDatabaseTests(unittest.TestCase):
    def test_loopback_translation_preserves_credentials_port_database_and_options(self):
        dsn = "postgresql://rag:p%40ss%24word@127.0.0.1:5433/RAG_SERVICE?sslmode=require"
        actual = conninfo_to_dict(database_url_for_container(dsn, "host.docker.internal"))
        expected = conninfo_to_dict(dsn)
        expected["host"] = "host.docker.internal"
        self.assertEqual(actual, expected)

    def test_remote_database_and_non_container_commands_remain_unchanged(self):
        remote = "postgresql://rag:password@db.example:5432/knowledge"
        self.assertEqual(database_url_for_container(remote, "host.docker.internal"), remote)
        local = "postgresql://rag:password@localhost:5433/RAG_SERVICE"
        self.assertEqual(database_url_for_container(local, ""), local)

    def test_ipv6_loopback_and_explicit_hostaddr(self):
        dsn = "host=::1 hostaddr=127.0.0.1 port=5433 dbname=RAG_SERVICE user=rag"
        actual = conninfo_to_dict(database_url_for_container(dsn, "host.docker.internal"))
        self.assertEqual(actual["host"], "host.docker.internal")
        self.assertNotIn("hostaddr", actual)


if __name__ == "__main__":
    unittest.main()
