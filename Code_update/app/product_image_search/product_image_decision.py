"""Combine vector similarity and Gemini visual verification scores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.product_image_search.product_visual_verifier import ProductVisualMatch


DecisionConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class ProductImageDecision:
    ranked_matches: list[dict[str, Any]]
    confidence: DecisionConfidence
    verification_applied: bool


class ProductImageDecisionEngine:
    def __init__(
        self,
        *,
        vector_weight: float,
        visual_weight: float,
        verify_min_score: float,
        high_confidence: float,
        medium_confidence: float,
        visual_min_score: float = 0.50,
    ):
        if vector_weight < 0 or visual_weight < 0:
            raise ValueError("Trọng số nhận diện không được âm.")
        total_weight = vector_weight + visual_weight
        if total_weight <= 0:
            raise ValueError("Tổng trọng số nhận diện phải lớn hơn 0.")
        if not 0 <= verify_min_score <= 1:
            raise ValueError("Ngưỡng kiểm chứng phải nằm trong khoảng 0 đến 1.")
        if not 0 <= visual_min_score <= 1:
            raise ValueError("Ngưỡng Visual phải nằm trong khoảng 0 đến 1.")
        if not 0 <= medium_confidence <= high_confidence <= 1:
            raise ValueError("Ngưỡng tin cậy không hợp lệ.")

        self.vector_weight = vector_weight / total_weight
        self.visual_weight = visual_weight / total_weight
        self.verify_min_score = verify_min_score
        self.visual_min_score = visual_min_score
        self.high_confidence = high_confidence
        self.medium_confidence = medium_confidence

    def decide(
        self,
        matches: list[dict[str, Any]],
        visual_matches: list[ProductVisualMatch],
    ) -> ProductImageDecision:
        if not matches or not visual_matches:
            return ProductImageDecision(
                ranked_matches=list(matches),
                confidence="low",
                verification_applied=False,
            )

        visual_by_code = {
            item.product_code: item.visual_score
            for item in visual_matches
        }
        verified: list[dict[str, Any]] = []
        for match in matches:
            product_code = str(match.get("product_code") or "").strip()
            visual_score = visual_by_code.get(product_code)
            if visual_score is None:
                continue
            vector_score = float(match.get("similarity") or 0.0)
            final_score = (
                vector_score * self.vector_weight
                + visual_score * self.visual_weight
            )
            verified.append(
                {
                    **match,
                    "vector_similarity": vector_score,
                    "visual_similarity": visual_score,
                    "similarity": final_score,
                }
            )

        eligible = [
            match
            for match in verified
            if (
                float(match["similarity"]) >= self.verify_min_score
                and float(match["visual_similarity"])
                >= self.visual_min_score
            )
        ]
        if not eligible:
            return ProductImageDecision(
                ranked_matches=list(matches),
                confidence="low",
                verification_applied=True,
            )

        eligible.sort(
            key=lambda item: float(item["similarity"]),
            reverse=True,
        )
        eligible_codes = {
            str(match.get("product_code") or "")
            for match in eligible
        }
        remaining = [
            match
            for match in matches
            if str(match.get("product_code") or "") not in eligible_codes
        ]
        ranked = [*eligible, *remaining]
        best_score = float(ranked[0]["similarity"])
        if best_score >= self.high_confidence:
            confidence: DecisionConfidence = "high"
        elif best_score >= self.medium_confidence:
            confidence = "medium"
        else:
            confidence = "low"

        return ProductImageDecision(
            ranked_matches=ranked,
            confidence=confidence,
            verification_applied=True,
        )


