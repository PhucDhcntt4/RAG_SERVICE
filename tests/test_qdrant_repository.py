import unittest

from app.qdrant_repository import QdrantRepository


class QdrantRepositoryTests(unittest.TestCase):
    def test_document_ids_are_serialized_without_javascript_rounding(self):
        identifier = 2630197296482906833
        row = QdrantRepository._document_row({
            "document_id": identifier,
            "source_key": "fixture",
            "title": "Fixture",
            "category": "test",
        }, 1)
        self.assertEqual(row["id"], "2630197296482906833")
        self.assertNotEqual(row["id"], str(int(float(identifier))))


if __name__ == "__main__":
    unittest.main()
