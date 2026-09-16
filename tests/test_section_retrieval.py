import unittest
from unittest.mock import Mock, patch

from app.chat import AnswerDraft, GeminiChat
from app.models import ChatRequest, SearchRequest
from app.retrieval import (
    condense_part_overview,
    heading_context_match,
    needs_section_context,
    section_scope,
    select_section_scopes,
)
from tests.test_service import config, make_service, sample_row


def row(index, content, scope="❖ Staff:"):
    return sample_row(chunk_id=index + 100, document_id=7, chunk_index=index,
                      content=content, section_path=["Handbook", "4. Policy", "a. Rules:", scope])


class SectionRetrievalTests(unittest.TestCase):
    def test_policy_question_recovers_siblings_and_excludes_unrelated_seed_sections(self):
        title = "2. CHÍNH SÁCH GÓI VAY KHÔNG LÃI SUẤT"
        paths = ["2.1. Mục đích:", "2.2. Đối tượng áp dụng:", "2.3. Gói vay:",
                 "2.4. Hình thức thanh toán:", "2.4. Hình thức thanh toán:",
                 "2.4. Hình thức thanh toán:", "2.5. Quy trình vay:", "2.6. Giấy tờ:"]
        expanded = [sample_row(document_id=7, chunk_id=i + 1, chunk_index=i,
                               section_path=["Handbook", title, path], content=f"Evidence {i}")
                    for i, path in enumerate(paths)]
        irrelevant = [sample_row(document_id=7, chunk_id=20 + i, chunk_index=20 + i,
                                 section_path=["Handbook", heading], content="Unrelated")
                      for i, heading in enumerate(["9. TRI ÂN THÂM NIÊN", "12. THIỆN NGUYỆN", "14. HỌC BỔNG"])]
        query = "Chính sách và lãi suất của gói vay nội bộ là gì?"
        self.assertTrue(needs_section_context(query))
        service = make_service(config(rag_max_context_chars=30000))
        service.repository.search.return_value = [expanded[0], expanded[2], *irrelevant]
        service.repository.expand_sections.return_value = expanded
        with patch("app.chat.genai.Client"):
            chat = GeminiChat(service.settings)
        chat.generate = Mock(return_value=AnswerDraft(sufficient=True, statements=[
            {"text": "Summary from evidence", "citations": [1]},
        ]))
        chat.answer(ChatRequest(query=query, categories=["company"]), service)
        self.assertEqual(service.repository.expand_sections.call_args.args[1],
                         [(7, ("Handbook", title))])
        payload = chat.generate.call_args.args[1]
        for item in expanded:
            self.assertIn(item["content"], payload["context"])
        self.assertNotIn("Unrelated", payload["context"])
        self.assertTrue(payload["retrieval_coverage"]["selected_sections_complete"])
        self.assertFalse(payload["retrieval_coverage"]["truncated"])

    def test_parent_scope_requires_an_existing_matching_numbered_ancestor(self):
        self.assertEqual(section_scope(["Title", "2. Loans", "2.3. Limits"], include_parent=True),
                         ("Title", "2. Loans"))
        self.assertEqual(section_scope(["Title", "20. Other", "2.3. Limits"], include_parent=True),
                         ("Title", "20. Other", "2.3. Limits"))
        self.assertEqual(section_scope(["Title", "2. Loans", "2.3. Limits"]),
                         ("Title", "2. Loans", "2.3. Limits"))

    def test_policy_scopes_support_multiple_topics_and_semantic_fallback(self):
        seeds = [sample_row(document_id=7, chunk_id=1, section_path=["Title", "2. Nghỉ phép"]),
                 sample_row(document_id=7, chunk_id=2, section_path=["Title", "3. Bảo hiểm"]),
                 sample_row(document_id=7, chunk_id=3, section_path=["Title", "4. Other"])]
        scopes = select_section_scopes(seeds, "Chính sách nghỉ phép và bảo hiểm")
        self.assertEqual(len(scopes), 2)
        self.assertEqual(len(select_section_scopes(seeds, "Chính sách phúc lợi nội bộ")), 3)

    def test_table_intent_and_heading_scope_are_generic(self):
        for query in ("bảng tính lỗi abc", "bảng tính lõi ABC", "bảng giá", "bảng định mức",
                      "Mức thưởng phạt ABC khi đi trễ là gì?",
                      "Gói vay nghĩa tình bao gồm những gì?", "giải thích cách tính tiền thưởng"):
            self.assertTrue(needs_section_context(query), query)
        for query in ("cửa hàng ở đâu", "trả bằng tiền mặt", "đi trễ 20 phút"):
            self.assertFalse(needs_section_context(query), query)
        self.assertEqual(section_scope(["Title", "4. Policy", "a. Rules:", "❖ Staff:"]),
                         ("Title", "4. Policy"))
        self.assertEqual(section_scope(["Title", "4.2. Detail", "❖ Staff:"]),
                         ("Title", "4.2. Detail"))

    def test_explanation_query_selects_abc_parent_and_drops_other_sections(self):
        abc = sample_row(document_id=7, chunk_id=1,
                         section_path=["Handbook", "4. QUY ĐỊNH VỀ THƯỞNG PHẠT ABC",
                                       "a. Thưởng phạt ABC:", "❖ Nhân viên:"])
        unrelated = sample_row(document_id=7, chunk_id=2,
                               section_path=["Handbook", "3. THỜI GIAN LÀM VIỆC"])
        scopes = select_section_scopes(
            [abc, unrelated], "Mức thưởng phạt ABC khi đi trễ là gì?",
        )
        self.assertEqual(scopes, [(7, ("Handbook", "4. QUY ĐỊNH VỀ THƯỞNG PHẠT ABC"))])

    def test_generic_benefit_query_selects_broad_part(self):
        seeds = [
            row(10, "Retirement benefits", scope="10.4. Phúc lợi sau nghỉ hưu"),
            row(11, "Health benefits", scope="6. Chăm sóc sức khỏe"),
        ]
        for seed in seeds:
            seed["section_path"] = [
                "PHẦN III: CHÍNH SÁCH PHÚC LỢI", *seed["section_path"][2:]
            ]
        self.assertTrue(needs_section_context("các phúc lợi được nhận"))
        self.assertEqual(
            select_section_scopes(seeds, "các phúc lợi được nhận"),
            [(7, ("PHẦN III: CHÍNH SÁCH PHÚC LỢI",))],
        )

    def test_exact_child_scope_removes_weaker_broad_ancestor(self):
        exact = sample_row(
            document_id=7, chunk_id=1,
            section_path=[
                "PHẦN I: GIỚI THIỆU CHUNG VỀ CÔNG TY",
                "1.2. Tầm nhìn – Sứ mệnh – Giá trị cốt lõi",
                "1.2.1. Tầm nhìn",
            ],
        )
        broad = sample_row(
            document_id=7, chunk_id=2,
            section_path=[
                "PHẦN I: GIỚI THIỆU CHUNG VỀ CÔNG TY",
                "1.3. Nhận diện thương hiệu",
            ],
        )
        scopes = select_section_scopes(
            [exact, broad],
            "tầm nhìn, sứ mệnh và giá trị cốt lõi của công ty Đông Hải là gì?",
        )
        self.assertEqual(scopes, [(
            7,
            ("PHẦN I: GIỚI THIỆU CHUNG VỀ CÔNG TY",
             "1.2. Tầm nhìn – Sứ mệnh – Giá trị cốt lõi"),
        )])

    def test_child_scope_pruning_is_generic_for_other_topics(self):
        loan = sample_row(
            document_id=7, chunk_id=1,
            section_path=[
                "PHAN III: CHINH SACH PHUC LOI",
                "2. Goi vay toi thieu toi da lai suat",
                "2.3. Han muc vay",
            ],
        )
        broad = sample_row(
            document_id=7, chunk_id=2,
            section_path=[
                "PHAN III: CHINH SACH PHUC LOI",
                "9. Hoc bong khuyen hoc",
            ],
        )
        scopes = select_section_scopes(
            [loan, broad],
            "giai thich chinh sach phuc loi goi vay toi thieu toi da va lai suat la gi",
        )
        self.assertEqual(scopes, [(
            7,
            ("PHAN III: CHINH SACH PHUC LOI", "2. Goi vay toi thieu toi da lai suat"),
        )])

    def test_short_heading_query_triggers_structural_context_without_magic_phrase(self):
        rows = [sample_row(
            document_id=7,
            section_path=[
                "PHAN I: GIOI THIEU CHUNG VE CONG TY",
                "1.2. Tam nhin Su menh Gia tri cot loi",
                "1.2.2. Su menh",
            ],
        )]
        query = "tam nhin su menh gia tri cot loi"
        self.assertFalse(needs_section_context(query))
        self.assertTrue(heading_context_match(rows, query))
        self.assertEqual(select_section_scopes(rows, query), [(
            7,
            ("PHAN I: GIOI THIEU CHUNG VE CONG TY",
             "1.2. Tam nhin Su menh Gia tri cot loi"),
        )])

    def test_heading_match_rejects_weak_overlap(self):
        rows = [sample_row(
            document_id=7,
            section_path=["HANDBOOK", "1.2. Tam nhin Su menh Gia tri cot loi"],
        )]
        self.assertFalse(heading_context_match(rows, "thong tin cong ty"))

    def test_search_auto_expands_a_strong_heading_match(self):
        service = make_service()
        parent = "1.2. Tam nhin Su menh Gia tri cot loi"
        seed = sample_row(
            document_id=7, chunk_id=2,
            section_path=["PHAN I: GIOI THIEU", parent, "1.2.2. Su menh"],
            content="Su menh",
        )
        service.repository.search.return_value = [seed]
        service.repository.expand_sections.return_value = [
            sample_row(document_id=7, chunk_id=1,
                       section_path=["PHAN I: GIOI THIEU", parent, "1.2.1. Tam nhin"],
                       content="Tam nhin"),
            seed,
            sample_row(document_id=7, chunk_id=3,
                       section_path=["PHAN I: GIOI THIEU", parent, "1.2.3. Gia tri cot loi"],
                       content="Tin, Tam, Nhan"),
        ]

        result = service.search(SearchRequest(
            query="tam nhin su menh gia tri cot loi",
        ))

        self.assertEqual(
            service.repository.expand_sections.call_args.args[1],
            [(7, ("PHAN I: GIOI THIEU", parent))],
        )
        self.assertEqual(len(result["sources"]), 3)
        self.assertIn("Tin, Tam, Nhan", result["content"])

    def test_manager_list_prefers_complete_section_over_whole_document(self):
        service = make_service(config(rag_max_context_chars=30000))
        path = [
            "PHẦN I: GIỚI THIỆU CHUNG VỀ CÔNG TY",
            "1.5. Cơ cấu tổ chức:",
            "1.5.2. Danh sách Quản lý:",
        ]
        first = sample_row(
            document_id=52, chunk_id=1706, chunk_index=21,
            heading=path[-1], section_path=path,
            content="Dòng 2: 1 | Quản lý A\nDòng 15: 14 | Quản lý N",
        )
        second = sample_row(
            document_id=52, chunk_id=1707, chunk_index=22,
            heading=path[-1], section_path=path,
            content="Dòng 16: 15 | Quản lý O\nDòng 26: 25 | Quản lý Y",
        )
        other = sample_row(
            document_id=52, chunk_id=1798, chunk_index=113,
            heading="❖ Dành cho cấp bậc Quản lý – Trưởng ca:",
            section_path=[
                "PHẦN II: QUY ĐỊNH CÔNG TY",
                "4. QUY ĐỊNH VỀ THƯỞNG PHẠT ABC",
                "a. Thưởng phạt ABC:",
                "❖ Dành cho cấp bậc Quản lý – Trưởng ca:",
            ],
            content="Quy định thưởng phạt không thuộc danh sách nhân sự.",
        )
        service.repository.search.return_value = [second, first, other]
        service.repository.expand_sections.return_value = [first, second]

        query = "danh sách các quản lý hiện nay"
        self.assertTrue(heading_context_match([first, second], query))
        result = service.search(
            SearchRequest(query=query), expand_documents=True,
        )

        self.assertEqual(
            service.repository.expand_sections.call_args.args[1],
            [(52, tuple(path))],
        )
        service.repository.expand_documents.assert_not_called()
        self.assertIn("1 | Quản lý A", result["content"])
        self.assertIn("25 | Quản lý Y", result["content"])
        self.assertTrue(result["retrieval_coverage"]["selected_sections_complete"])

    def test_pruning_preserves_independent_scopes_and_documents(self):
        seeds = [
            sample_row(document_id=7, chunk_id=1,
                       section_path=["Handbook", "1. Vision"]),
            sample_row(document_id=7, chunk_id=2,
                       section_path=["Handbook", "2. Mission"]),
            sample_row(document_id=8, chunk_id=3,
                       section_path=["Handbook", "1. Vision"]),
        ]
        scopes = select_section_scopes(seeds, "giai thich vision mission")
        self.assertEqual(scopes, [
            (7, ("Handbook", "1. Vision")),
            (7, ("Handbook", "2. Mission")),
            (8, ("Handbook", "1. Vision")),
        ])

    def test_part_overview_keeps_one_chunk_per_direct_child(self):
        rows = []
        for chunk_id, child in ((1, "1. Tiết kiệm"), (2, "1. Tiết kiệm"),
                                (3, "2. Gói vay"), (4, "2. Gói vay")):
            item = row(chunk_id, f"content {chunk_id}")
            item["section_path"] = ["PHẦN III: CHÍNH SÁCH PHÚC LỢI", child, "Mục con"]
            rows.append(item)
        result = condense_part_overview(
            rows, [(7, ("PHẦN III: CHÍNH SÁCH PHÚC LỢI",))]
        )
        self.assertEqual([item["chunk_id"] for item in result], [101, 103])

    def test_table_question_reads_full_section_notes_then_llm_writes_answer(self):
        service = make_service(config(rag_max_context_chars=30000))
        service.repository.search.return_value = [row(10, "Late | A"), row(20, "Manager", "❖ Managers:")]
        service.repository.expand_sections.return_value = [row(10, "Late | A"),
                                                          row(30, "One A: reward 80 units.")]
        with patch("app.chat.genai.Client"):
            chat = GeminiChat(service.settings)
        chat.generate = Mock(return_value=AnswerDraft(sufficient=True, statements=[
            {"text": "One A: reward 80 units.", "citations": [2]},
        ]))
        result = chat.answer(ChatRequest(query="bảng tính lỗi abc", categories=["company"]), service)
        self.assertEqual(result["answer"], "One A: reward 80 units. [S2]")
        args = service.repository.expand_sections.call_args.args
        self.assertEqual(args[1], [(7, ("Handbook", "4. Policy"))])
        self.assertEqual(args[3], ["company"])
        service.repository.expand_documents.assert_not_called()
        payload = chat.generate.call_args.args[1]
        self.assertIn("One A: reward 80 units.", payload["context"])
        self.assertTrue(payload["retrieval_coverage"]["selected_sections_complete"])
        self.assertFalse(payload["retrieval_coverage"]["selected_documents_complete"])

    def test_cap_and_missing_heading_do_not_claim_complete(self):
        service = make_service(config(rag_overview_max_chunks=1))
        service.repository.search.return_value = [row(10, "seed")]
        service.repository.expand_sections.return_value = [row(10, "A"), row(11, "B")]
        result = service.search(SearchRequest(query="bảng lỗi"), expand_sections=True)
        self.assertTrue(result["retrieval_coverage"]["truncated"])
        self.assertFalse(result["retrieval_coverage"]["selected_sections_complete"])
        service.repository.search.return_value = [sample_row(content="plain")]
        service.repository.expand_sections.reset_mock()
        service.search(SearchRequest(query="bảng lỗi"), expand_sections=True)
        service.repository.expand_sections.assert_not_called()

    def test_context_budget_never_sends_half_a_table_row(self):
        service = make_service(config(rag_max_context_chars=500))
        first = "Dòng 1: Cột 1: Short condition | Cột 2: A"
        second = "Dòng 2: Cột 1: " + "long condition " * 60 + " | Cột 2: Z"
        service.repository.search.return_value = [row(1, "Bảng, trang PDF 29.\n" + first + "\n" + second)]
        result = service.search(SearchRequest(query="bảng lỗi"))
        self.assertIn(first, result["content"])
        self.assertNotIn("Dòng 2:", result["content"])
        self.assertTrue(result["retrieval_coverage"]["truncated"])
        service.repository.search.return_value = [row(1, "Bảng, trang PDF 29.\n" + second)]
        result = service.search(SearchRequest(query="bảng lỗi"))
        self.assertFalse(result["success"])
        self.assertTrue(result["retrieval_coverage"]["truncated"])


if __name__ == "__main__":
    unittest.main()
