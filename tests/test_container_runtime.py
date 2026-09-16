import unittest
from unittest.mock import patch

from app.container_runtime import healthcheck


class ContainerRuntimeTests(unittest.TestCase):
    @patch("app.container_runtime.urlopen")
    @patch("app.container_runtime.Settings.load")
    def test_healthcheck_uses_qdrant_service_readiness(self, load, urlopen):
        load.return_value.search_api_key.get_secret_value.return_value = "s" * 32
        response = urlopen.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = (
            b'{"status":"ready","vector_provider":"qdrant"}'
        )
        self.assertEqual(healthcheck(), 0)


if __name__ == "__main__":
    unittest.main()
