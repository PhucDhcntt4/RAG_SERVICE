"""Find visually similar products from the existing Qdrant image collection."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import logging
from pathlib import PurePath
from typing import Any

import httpx

from app.product_image_search.gemini_vision_client import (
    GeminiVisionClient,
    GeminiVisionError,
)
from app.product_image_search.image_embedder import (
    ImageEmbeddingError,
    InvalidProductImageError,
    ProductImageEmbedder,
)
from app.product_image_search.qdrant_image_client import QdrantImageClient, QdrantImageError
from app.product_image_search.product_visual_verifier import (
    ProductVisualCandidate,
    ProductVisualVerificationError,
    ProductVisualVerifier,
)
from app.config import Settings
from app.product_image_search.models import (
    ProductImageQuery,
    ProductImageSearchResponse,
    ProductResultMeta,
    ProductSource,
)
from app.product_image_search.image_processing_service import (
    ImageProcessingError,
    ImageProcessingService,
)
from app.product_image_search.product_category_mapping import load_product_category_mapping
from app.product_image_search.product_image_decision import ProductImageDecisionEngine
from app.product_image_search.product_image_downloader import (
    ProductImageDownloadError,
    ProductImageDownloader,
)
from app.product_image_search.product_mapper import ProductMapper, safe_http_url
from app.product_repository import ProductRepository

LOGGER = logging.getLogger(__name__)


class ProductImageSearchError(RuntimeError):
    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class ProductImageService:
    ALLOWED_MEDIA_TYPES = {
        "image/jpeg",
        "image/png",
        "image/webp",
    }

    def __init__(
        self,
        settings: Settings,
        embedder: ProductImageEmbedder,
        qdrant: QdrantImageClient,
        products: ProductRepository,
        vision_client: GeminiVisionClient | None = None,
        image_processor: ImageProcessingService | None = None,
        category_mapping: Mapping[str, Sequence[str]] | None = None,
        visual_verifier: ProductVisualVerifier | None = None,
        image_downloader: ProductImageDownloader | None = None,
        decision_engine: ProductImageDecisionEngine | None = None,
    ):
        self.settings = settings
        self.embedder = embedder
        self.qdrant = qdrant
        self.products = products
        self.vision_client = vision_client
        self.image_processor = image_processor
        self.visual_verifier = visual_verifier
        self.image_downloader = image_downloader
        self.decision_engine = decision_engine
        source_mapping = (
            load_product_category_mapping()
            if category_mapping is None
            else category_mapping
        )
        self.category_mapping = {
            str(category).strip().lower(): tuple(
                str(product_type).strip()
                for product_type in product_types
                if str(product_type).strip()
            )
            for category, product_types in source_mapping.items()
            if str(category).strip()
        }

    @staticmethod
    def _safe_filename(value: str) -> str:
        normalized = str(value or "image").replace("\\", "/")
        return PurePath(normalized).name[:255] or "image"

    @staticmethod
    def _best_matches(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best_by_product: dict[str, dict[str, Any]] = {}
        for point in points:
            payload = point.get("payload")
            if not isinstance(payload, dict):
                continue
            product_code = str(payload.get("product_code") or "").strip()
            if not product_code:
                continue
            try:
                similarity = float(point.get("score"))
            except (TypeError, ValueError):
                continue
            current = best_by_product.get(product_code)
            if current is None or similarity > current["similarity"]:
                best_by_product[product_code] = {
                    "product_code": product_code,
                    "similarity": similarity,
                    "color": str(payload.get("color") or "").strip(),
                    "product_type": str(
                        payload.get("product_type") or ""
                    ).strip(),
                    "image_checksum": str(
                        payload.get("image_checksum") or ""
                    ).strip(),
                }
        return sorted(
            best_by_product.values(),
            key=lambda item: item["similarity"],
            reverse=True,
        )

    @staticmethod
    def _matched_image_url(detail: dict, match: dict[str, Any]) -> str | None:
        payload = ProductMapper.payload(detail)
        images = payload.get("images") or []
        expected_checksum = match.get("image_checksum")
        expected_color = str(match.get("color") or "").casefold()

        for image in images:
            if not isinstance(image, dict):
                continue
            checksum_matches = (
                expected_checksum
                and image.get("image_checksum") == expected_checksum
            )
            color_matches = (
                expected_color
                and str(image.get("color") or "").casefold() == expected_color
            )
            if checksum_matches or color_matches:
                url = safe_http_url(image.get("source_url"))
                if url:
                    return url
        return None

    async def _load_product(
        self,
        match: dict[str, Any],
    ) -> ProductSource | None:
        detail = await asyncio.to_thread(
            self.products.get_product,
            match["product_code"],
        )
        if detail is None:
            return None

        source = ProductMapper.to_source(detail, "")
        if source.status and source.status.upper() != "ACTIVE":
            return None

        matched_image_url = self._matched_image_url(detail, match)
        image_urls = list(source.image_urls)
        if matched_image_url:
            image_urls = [
                matched_image_url,
                *(url for url in image_urls if url != matched_image_url),
            ]

        return source.model_copy(
            update={
                "image_urls": image_urls,
                "image_url": image_urls[0] if image_urls else source.image_url,
                "image_similarity": match["similarity"],
                "matched_image_url": matched_image_url,
                "matched_color": match["color"],
            }
        )

    async def _prepare_embedding_image(
        self,
        *,
        data: bytes,
        media_type: str,
    ) -> tuple[bytes, str | None, bool]:
        """Phân tích và crop sản phẩm trước khi tạo OpenCLIP embedding."""

        if not self.settings.product_vision_enabled:
            return data, None, False

        if self.vision_client is None or self.image_processor is None:
            LOGGER.warning(
                "PRODUCT_VISION_ENABLED=true nhưng pipeline Vision chưa được khởi tạo."
            )
            return data, None, False

        try:
            analysis = await self.vision_client.analyze(
                data=data,
                media_type=media_type,
            )
        except GeminiVisionError as exc:
            # Gemini lỗi thì vẫn cho tìm bằng ảnh gốc.
            LOGGER.warning(
                "Gemini Vision thất bại, sử dụng ảnh gốc: %s",
                exc,
            )
            return data, None, False

        if not analysis.product_detected:
            raise ProductImageSearchError(
                "Không phát hiện sản phẩm phù hợp trong ảnh.",
                status_code=422,
            )

        if analysis.bounding_box is None:
            return data, analysis.category, True

        try:
            processed = await asyncio.to_thread(
                self.image_processor.process,
                data,
                analysis.bounding_box,
            )
        except ImageProcessingError as exc:
            # Bounding box/crop lỗi thì quay lại ảnh gốc.
            LOGGER.warning(
                "Không thể crop ảnh, sử dụng ảnh gốc: %s",
                exc,
            )
            return data, analysis.category, True

        return processed.data, analysis.category, True

    async def _search_candidates(
        self,
        *,
        vector: list[float],
        category: str | None,
    ) -> list[dict[str, Any]]:
        """Search globally and, when possible, inside the detected category."""

        global_points, category_points = await asyncio.gather(
            self._search_global_candidates(vector),
            self._search_category_candidates(
                vector=vector,
                category=category,
            ),
        )
        return [*global_points, *category_points]

    async def _embed_image(self, data: bytes) -> list[float]:
        try:
            return await asyncio.to_thread(self.embedder.embed, data)
        except InvalidProductImageError as exc:
            raise ProductImageSearchError(str(exc), status_code=422) from exc
        except ImageEmbeddingError as exc:
            raise ProductImageSearchError(str(exc), status_code=503) from exc

    async def _search_global_candidates(
        self,
        vector: list[float],
    ) -> list[dict[str, Any]]:
        try:
            return await self.qdrant.search(
                vector,
                limit=self.settings.product_image_candidate_limit,
                score_threshold=None,
            )
        except QdrantImageError as exc:
            raise ProductImageSearchError(str(exc), status_code=503) from exc

    async def _search_category_candidates(
        self,
        *,
        vector: list[float],
        category: str | None,
    ) -> list[dict[str, Any]]:
        product_types = self.category_mapping.get(
            str(category or "").strip().lower(),
            (),
        )
        if not product_types:
            return []

        try:
            return await self.qdrant.search(
                vector,
                limit=self.settings.product_image_candidate_limit,
                score_threshold=None,
                product_types=product_types,
            )
        except QdrantImageError as exc:
            LOGGER.warning(
                "Tìm kiếm Qdrant theo nhóm '%s' thất bại, "
                "sử dụng kết quả toàn catalog: %s",
                category,
                exc,
            )
            return []

    def _should_fast_accept(
        self,
        ranked: list[dict[str, Any]],
    ) -> bool:
        if not ranked:
            return False

        top_score = float(ranked[0]["similarity"])
        second_score = (
            float(ranked[1]["similarity"])
            if len(ranked) > 1
            else 0.0
        )
        return (
            top_score >= self.settings.product_image_fast_accept_score
            and top_score - second_score
            >= self.settings.product_image_fast_accept_margin
        )

    @staticmethod
    def _format_top_matches(
        ranked: list[dict[str, Any]],
        *,
        limit: int = 5,
    ) -> str:
        if not ranked:
            return "none"
        rows: list[str] = []
        for match in ranked[:limit]:
            product_code = str(match.get("product_code") or "unknown")
            try:
                score = float(match.get("similarity"))
                rows.append(f"{product_code}={score:.3f}")
            except (TypeError, ValueError):
                rows.append(f"{product_code}=invalid")
        return ", ".join(rows)

    def _select_verification_candidates(
        self,
        *,
        original_ranked: list[dict[str, Any]],
        refined_ranked: list[dict[str, Any]],
        final_ranked: list[dict[str, Any]],
        category: str | None,
    ) -> list[dict[str, Any]]:
        limit = self.settings.product_vision_verify_top_k
        original_slots = min(
            self.settings.product_vision_original_candidate_slots,
            limit,
        )
        refined_slots = max(0, limit - original_slots)

        expected_types = {
            product_type.casefold()
            for product_type in self.category_mapping.get(
                str(category or "").strip().lower(),
                (),
            )
        }
        same_category_original = [
            match
            for match in original_ranked
            if str(match.get("product_type") or "").casefold()
            in expected_types
        ]
        original_pool = same_category_original or original_ranked

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(matches: Sequence[dict[str, Any]]) -> None:
            for match in matches:
                product_code = str(match.get("product_code") or "").strip()
                if not product_code or product_code in seen or len(selected) >= limit:
                    continue
                seen.add(product_code)
                selected.append(match)

        add(original_pool[:original_slots])
        add(refined_ranked[:refined_slots])
        add([*refined_ranked, *original_pool, *final_ranked])
        return selected

    async def _load_verification_reference(
        self,
        match: dict[str, Any],
    ) -> tuple[ProductSource | None, ProductVisualCandidate | None]:
        try:
            product = await self._load_product(match)
        except httpx.HTTPError as exc:
            LOGGER.warning(
                "Không thể tải sản phẩm %s để kiểm chứng: %s",
                match.get("product_code"),
                exc,
            )
            return None, None
        if product is None:
            return None, None

        image_url = product.matched_image_url or product.image_url
        if not image_url or self.image_downloader is None:
            return product, None
        try:
            downloaded = await self.image_downloader.download(image_url)
        except ProductImageDownloadError as exc:
            LOGGER.warning(
                "Bỏ qua ảnh tham chiếu của %s: %s",
                product.product_code,
                exc,
            )
            return product, None

        return product, ProductVisualCandidate(
            product_code=product.product_code,
            data=downloaded.data,
            media_type=downloaded.media_type,
        )

    async def _verify_and_rerank(
        self,
        *,
        query_data: bytes,
        query_media_type: str,
        ranked: list[dict[str, Any]],
        verification_candidates: list[dict[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, ProductSource],
        bool | None,
    ]:
        if (
            not self.settings.product_visual_verification_enabled
            or self.visual_verifier is None
            or self.image_downloader is None
            or self.decision_engine is None
            or not ranked
        ):
            return ranked, {}, None

        candidate_matches = verification_candidates[
            : self.settings.product_vision_verify_top_k
        ]
        LOGGER.debug(
            "Ứng viên gửi Visual Verification: total=%d, top%d=[%s].",
            len(candidate_matches),
            self.settings.product_vision_verify_top_k,
            self._format_top_matches(
                candidate_matches,
                limit=self.settings.product_vision_verify_top_k,
            ),
        )
        loaded = await asyncio.gather(
            *(
                self._load_verification_reference(match)
                for match in candidate_matches
            )
        )
        preloaded = {
            product.product_code: product
            for product, _reference in loaded
            if product is not None
        }
        references = [
            reference
            for _product, reference in loaded
            if reference is not None
        ]
        if not references:
            LOGGER.warning(
                "Không có ảnh tham chiếu hợp lệ; "
                "giữ nguyên thứ tự Qdrant."
            )
            return ranked, preloaded, None

        try:
            visual_matches = await self.visual_verifier.verify(
                query_data=query_data,
                query_media_type=query_media_type,
                candidates=references,
            )
        except ProductVisualVerificationError as exc:
            LOGGER.warning(
                "Gemini Visual Verification thất bại; "
                "giữ nguyên thứ tự Qdrant: %s",
                exc,
            )
            return ranked, preloaded, None

        decision = self.decision_engine.decide(ranked, visual_matches)
        LOGGER.debug(
            "Điểm Gemini Visual: [%s].",
            ", ".join(
                f"{match.product_code}={match.visual_score:.3f}"
                for match in visual_matches
            ) or "none",
        )
        LOGGER.info(
            "Visual Verification: candidates=%d, verified=%d, confidence=%s.",
            len(references),
            len(visual_matches),
            decision.confidence,
        )
        return (
            decision.ranked_matches,
            preloaded,
            decision.confidence == "low",
        )

    async def search(
        self,
        *,
        filename: str,
        media_type: str,
        data: bytes,
    ) -> ProductImageSearchResponse:
        if not self.settings.product_image_search_enabled:
            raise ProductImageSearchError(
                "Tìm kiếm sản phẩm bằng ảnh đang tắt.",
                status_code=503,
            )
        normalized_media_type = media_type.split(";", 1)[0].strip().lower()
        if normalized_media_type not in self.ALLOWED_MEDIA_TYPES:
            raise ProductImageSearchError(
                "Chỉ hỗ trợ ảnh JPEG, PNG và WebP.",
                status_code=415,
            )
        if not data:
            raise ProductImageSearchError("Ảnh tải lên không có dữ liệu.", status_code=422)
        if len(data) > self.settings.product_image_max_bytes:
            raise ProductImageSearchError(
                "Ảnh vượt quá dung lượng cho phép.",
                status_code=413,
            )

        LOGGER.debug(
            "Bắt đầu nhận diện: file=%s, media_type=%s, size_bytes=%d.",
            self._safe_filename(filename),
            normalized_media_type,
            len(data),
        )

        original_vector = await self._embed_image(data)
        global_points = await self._search_global_candidates(original_vector)
        global_ranked = self._best_matches(global_points)
        LOGGER.debug(
            "Qdrant nhanh: points=%d, sku=%d, top1=%.3f, top2=%.3f, "
            "top%d=[%s].",
            len(global_points),
            len(global_ranked),
            (
                float(global_ranked[0]["similarity"])
                if global_ranked else 0.0
            ),
            (
                float(global_ranked[1]["similarity"])
                if len(global_ranked) > 1 else 0.0
            ),
            self.settings.product_image_result_limit,
            self._format_top_matches(
                global_ranked,
                limit=self.settings.product_image_result_limit,
            ),
        )
        verification_data = data
        verification_media_type = normalized_media_type
        vision_succeeded = False
        detected_category: str | None = None
        refined_ranked: list[dict[str, Any]] = []

        if (
            not self.settings.product_vision_enabled
            or self._should_fast_accept(global_ranked)
        ):
            points = global_points
            if self.settings.product_vision_enabled and global_ranked:
                top_score = float(global_ranked[0]["similarity"])
                second_score = (
                    float(global_ranked[1]["similarity"])
                    if len(global_ranked) > 1
                    else 0.0
                )
                LOGGER.info(
                    "Bỏ qua Gemini Vision: top1=%.3f, margin=%.3f.",
                    top_score,
                    top_score - second_score,
                )
        else:
            LOGGER.info(
                "Kết quả Qdrant nhanh chưa đủ tin cậy; "
                "chuyển sang Gemini Vision."
            )
            embedding_data, detected_category, vision_succeeded = (
                await self._prepare_embedding_image(
                    data=data,
                    media_type=normalized_media_type,
                )
            )
            LOGGER.debug(
                "Gemini Vision: success=%s, category=%s, crop_applied=%s.",
                vision_succeeded,
                detected_category or "unknown",
                embedding_data != data,
            )

            if embedding_data == data:
                category_points = await self._search_category_candidates(
                    vector=original_vector,
                    category=detected_category,
                )
                points = [*global_points, *category_points]
                refined_ranked = self._best_matches(category_points)
            else:
                verification_data = embedding_data
                verification_media_type = "image/jpeg"
                refined_vector = await self._embed_image(embedding_data)
                refined_points = await self._search_candidates(
                    vector=refined_vector,
                    category=detected_category,
                )
                points = [*global_points, *refined_points]
                refined_ranked = self._best_matches(refined_points)

        ranked = self._best_matches(points)
        LOGGER.debug(
            "Xếp hạng sau truy xuất: points=%d, sku=%d, "
            "top1=%.3f, top%d=[%s].",
            len(points),
            len(ranked),
            float(ranked[0]["similarity"]) if ranked else 0.0,
            self.settings.product_image_result_limit,
            self._format_top_matches(
                ranked,
                limit=self.settings.product_image_result_limit,
            ),
        )
        preloaded_products: dict[str, ProductSource] = {}
        verification_low_confidence: bool | None = None
        if vision_succeeded and not self._should_fast_accept(ranked):
            verification_candidates = self._select_verification_candidates(
                original_ranked=global_ranked,
                refined_ranked=refined_ranked,
                final_ranked=ranked,
                category=detected_category,
            )
            (
                ranked,
                preloaded_products,
                verification_low_confidence,
            ) = await self._verify_and_rerank(
                query_data=verification_data,
                query_media_type=verification_media_type,
                ranked=ranked,
                verification_candidates=verification_candidates,
            )
            LOGGER.debug(
                "Xếp hạng sau Visual Verification: top%d=[%s].",
                self.settings.product_image_result_limit,
                self._format_top_matches(
                    ranked,
                    limit=self.settings.product_image_result_limit,
                ),
            )

        qualified = [
            match
            for match in ranked
            if match["similarity"] >= self.settings.product_image_min_score
        ]
        low_confidence = not qualified and bool(ranked)
        if verification_low_confidence is not None:
            low_confidence = verification_low_confidence
        if low_confidence:
            selected_matches = ranked[: min(3, self.settings.product_image_result_limit)]
        else:
            selected_matches = qualified
        # Fetch a few extra products because stale/inactive catalog entries may
        # be skipped after the image-vector query.
        detail_candidates = selected_matches[
            : self.settings.product_image_result_limit * 3
        ]
        async def load_product(match: dict[str, Any]) -> ProductSource | None:
            product_code = str(match.get("product_code") or "")
            preloaded = preloaded_products.get(product_code)
            if preloaded is not None:
                return preloaded.model_copy(
                    update={"image_similarity": match["similarity"]}
                )
            return await self._load_product(match)

        loaded = await asyncio.gather(
            *(load_product(match) for match in detail_candidates)
        )
        products = [product for product in loaded if product is not None]
        products = products[: self.settings.product_image_result_limit]

        total = len(products)
        LOGGER.debug(
            "Hoàn tất nhận diện: top_sku=%s, score=%.3f, "
            "low_confidence=%s, products=%d.",
            products[0].product_code if products else "none",
            float(ranked[0]["similarity"]) if ranked else 0.0,
            low_confidence,
            total,
        )
        return ProductImageSearchResponse(
            query_image=ProductImageQuery(
                filename=self._safe_filename(filename),
                media_type=normalized_media_type,
                size_bytes=len(data),
            ),
            products=products,
            result=ProductResultMeta(
                page=1,
                page_size=self.settings.product_image_result_limit,
                shown=total,
                total=total,
                total_pages=1 if total else 0,
                has_more=False,
            ),
            low_confidence=low_confidence,
            best_similarity=(ranked[0]["similarity"] if ranked else None),
        )

