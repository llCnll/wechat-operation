from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class AdapterError(Exception):
    """Safe domain error for transport and response-contract failures."""


class SourceContractError(AdapterError):
    pass


class SourceTransportError(AdapterError):
    pass


class SourceBusinessDeletedError(SourceTransportError):
    """The management API confirms that a post is no longer operable."""


@dataclass(frozen=True)
class CandidatePostDTO:
    topic_id: int
    content_id: str
    source_appid: str
    publish_time: datetime | None = None


class SourcePlatformClient(Protocol):
    def list_candidates(
        self, *, page: int, page_size: int, start_time: int, end_time: int
    ) -> list[CandidatePostDTO]: ...

    def get_content_card(self, content_id: str) -> dict: ...

    def get_post_metrics(self, topic_id: int) -> dict: ...

    def perform_confirmed_operation(self, *, topic_id: int, action: str, payload: dict) -> dict: ...


class OcrClient(Protocol):
    def extract_text(self, image_url: str) -> tuple[str, float]: ...


class AiReviewClient(Protocol):
    def evaluate(self, content: str, ocr_text: str) -> dict: ...
