import unittest
from dataclasses import replace
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

from app.embeddings import EmbeddingError
from app.chunking import Chunk
from app.main import create_app
from app.models import DocumentRequest
from app.pdf_chunking import prepare_pdf_document
from app.pdf_extract import PdfExtraction
from app.service import InvalidDocument, ServiceBusy
from tests.test_pdf_structure import line
from tests.test_service import config, make_service


def pdf_bytes(*, blank_page=False, encrypted=False):
    """Generate a text PDF locally; no fixture or external provider needed."""
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((50, 60), "BUILDING RULES", fontname="hebo", fontsize=22)
        page.insert_text((50, 100), "I. Security:", fontname="hebo", fontsize=13)
        page.insert_text((50, 130), "1.1. Entry:", fontname="hebo", fontsize=13)
        page.insert_text((50, 160), "a. Staff:", fontname="hebo", fontsize=13)
        page.insert_text((50, 190), "Synthetic policy: staff report to reception before entry.", fontsize=13)
        if blank_page:
            document.new_page()
        if encrypted:
            return document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                                    owner_pw="owner", user_pw="test")
        return document.tobytes()


def upload(client, data):
    return client.post(
        "/api/v1/documents/upload",
        headers={"Authorization": "Bearer " + "a" * 32},
        data={"source_key": "fixture/pdf", "title": "Fixture", "category": "company"},
        files={"file": ("rules.PDF", data, "application/pdf")},
    )


class PdfUploadTests(unittest.TestCase):
    def test_pdf_upload_uses_layout_once_and_preserves_file(self):
        service = make_service()
        raw = pdf_bytes()
        from app.pdf_extract import extract_pdf
        with TestClient(create_app(service.settings, service)) as client:
            with patch("app.service.extract_pdf", wraps=extract_pdf) as extractor:
                response = upload(client, raw)
        self.assertEqual(response.status_code, 200, response.text)
        extractor.assert_called_once()
        document, chunks = service.repository.replace.call_args.args[:2]
        self.assertEqual(document.category, "company")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].section_path, (
            "BUILDING RULES", "I. Security:", "1.1. Entry:", "a. Staff:",
        ))
        self.assertEqual(chunks[0].content, "Synthetic policy: staff report to reception before entry.")
        self.assertIn("BUILDING RULES > I. Security: > 1.1. Entry: > a. Staff:",
                      service.embedder.embed.call_args.args[0][0])
        self.assertIn("a. Staff:", document.text)
        self.assertNotIn("#", document.text)
        service.storage.save.assert_called_once_with(raw, "rules.PDF", origin="upload")

    def test_parent_content_inline_heading_and_page_break_are_preserved(self):
        label = line("a. Usage: ", bold=True)
        body = line("Seven floors.", x=250)
        inline = replace(label, text=label.text + body.text, spans=label.spans + body.spans)
        extraction = PdfExtraction((
            line("Introductory text. " * 30),
            line("II. Departments:", bold=True),
            line("2.1. Access:", bold=True),
            line("Parent scope."), inline,
            line("Continued on next page.", page=2),
            line("b. Guests:", bold=True, page=2), line("Guest instructions.", page=2),
        ), 2, ())
        text, chunks = prepare_pdf_document(extraction)
        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks[1].content, "Parent scope.")
        self.assertEqual(chunks[2].content, "Seven floors.\nContinued on next page.")
        self.assertEqual(chunks[2].section_path, ("II. Departments:", "2.1. Access:", "a. Usage:"))
        self.assertEqual(chunks[3].section_path[-1], "b. Guests:")
        self.assertIn("Seven floors.", text)

    def test_long_pdf_section_keeps_tail_and_never_overlaps_next_section(self):
        extraction = PdfExtraction((
            line("I. Long section:", bold=True), line("alpha " * 200 + "END_A"),
            line("II. Other section:", bold=True), line("ONLY_B"),
        ), 1, ())
        _, chunks = prepare_pdf_document(extraction, 200, 30)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(c.content) <= 200 for c in chunks))
        self.assertTrue(chunks[-2].content.endswith("END_A"))
        self.assertEqual(chunks[-1].content, "ONLY_B")
        self.assertTrue(all(c.section_path == ("I. Long section:",) for c in chunks[:-1]))

    def test_adaptive_pdf_preparation_keeps_whole_sections_for_semantic_stage(self):
        extraction = PdfExtraction((
            line("I. Long section:", bold=True), line("alpha " * 200 + "END_A"),
            line("II. Other section:", bold=True), line("ONLY_B"),
        ), 1, ())
        _, chunks = prepare_pdf_document(
            extraction, 200, 30, split_sections=False,
        )
        self.assertEqual(len(chunks), 2)
        self.assertGreater(len(chunks[0].content), 200)
        self.assertTrue(chunks[0].content.endswith("END_A"))
        self.assertEqual(chunks[1].content, "ONLY_B")

    def test_pdf_without_headings_still_indexes_body(self):
        _, chunks = prepare_pdf_document(PdfExtraction((line("Plain body."),), 1, ()))
        self.assertEqual(chunks[0].section_path, ())
        self.assertEqual(chunks[0].content, "Plain body.")

    def test_invalid_encrypted_and_partial_text_pdfs_do_not_write(self):
        for raw in (b"invalid", pdf_bytes(encrypted=True), pdf_bytes(blank_page=True)):
            with self.subTest(length=len(raw)):
                service = make_service()
                with TestClient(create_app(service.settings, service)) as client:
                    response = upload(client, raw)
                self.assertEqual(response.status_code, 422, response.text)
                service.embedder.embed.assert_not_called()
                service.storage.save.assert_not_called()
                service.repository.replace.assert_not_called()

    def test_pdf_limits_are_enforced_before_embedding(self):
        for settings in (
            config(rag_max_document_chars=100),
            config(rag_max_upload_bytes=1024),
        ):
            service = make_service(settings)
            with TestClient(create_app(service.settings, service)) as client:
                response = upload(client, pdf_bytes())
            self.assertIn(response.status_code, (413, 422))
            service.embedder.embed.assert_not_called()
            service.repository.replace.assert_not_called()

    def test_pdf_chunk_limit_cannot_be_bypassed_by_prepared_chunks(self):
        service = make_service(config(rag_max_chunks=1))
        with self.assertRaises(InvalidDocument):
            service.ingest(DocumentRequest(source_key="fixture", title="T", text="body"),
                           prepared_chunks=[Chunk(0, None, "a"), Chunk(1, None, "b")])
        service.embedder.embed.assert_not_called()

    def test_embedding_failure_keeps_existing_document_and_file(self):
        service = make_service()
        service.embedder.embed.side_effect = EmbeddingError("Synthetic failure")
        with TestClient(create_app(service.settings, service)) as client:
            response = upload(client, pdf_bytes())
        self.assertEqual(response.status_code, 503)
        service.repository.replace.assert_not_called()
        service.storage.save.assert_not_called()
        service.storage.remove.assert_not_called()

    def test_busy_pdf_parser_returns_service_busy(self):
        service = make_service(config(rag_max_concurrent_requests=1))
        with service.capacity():
            with self.assertRaises(ServiceBusy):
                service.prepare_upload("a.pdf", pdf_bytes())
        # Slot is usable again after the request exits.
        text, chunks = service.prepare_upload("a.pdf", pdf_bytes())
        self.assertTrue(text)
        self.assertTrue(chunks)


if __name__ == "__main__":
    unittest.main()
