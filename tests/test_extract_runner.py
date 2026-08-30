"""Model fallback and the extraction pass."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from touchstone.db.models import ExtractionMethod, ItemSpec, Listing
from touchstone.extract.llm import (
    DEFAULT_MODEL,
    ModelRejected,
    UmansExtractor,
    parse_model_output,
    validate_model,
)
from touchstone.extract.normalize import title_hash
from touchstone.extract.runner import correct_spec, pending_titles, run_extraction
from touchstone.extract.specs import SpecCandidate


@dataclass
class FakeUmans:
    """An OpenAI-compatible endpoint that returns whatever a test tells it to."""

    replies: list[str | None]
    status: int = 200
    calls: int = field(default=0, init=False)
    _server: HTTPServer | None = field(default=None, init=False)

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                index = min(fake.calls, len(fake.replies) - 1)
                reply = fake.replies[index] if fake.replies else None
                fake.calls += 1

                if fake.status != 200 or reply is None:
                    body = json.dumps({"error": "unavailable"}).encode()
                    self.send_response(fake.status if fake.status != 200 else 503)
                else:
                    body = json.dumps(
                        {"choices": [{"message": {"content": reply}}]}
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host!s}:{port!s}/v1"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def fake_umans() -> Iterator[FakeUmans]:
    fake = FakeUmans(replies=[])
    yield fake
    fake.stop()


class TestModelPolicy:
    def test_lab_models_are_rejected(self) -> None:
        """The operator's rule, enforced where the value enters rather than in a
        comment a later env change can ignore."""
        with pytest.raises(ModelRejected):
            validate_model("umans-glm-5.3-flash-lab")

    def test_lab_model_rejected_at_construction(self) -> None:
        with pytest.raises(ModelRejected):
            UmansExtractor(api_key="k", model="umans-qwen3.8-flash-next-lab")

    def test_lab_fallback_also_rejected(self) -> None:
        with pytest.raises(ModelRejected):
            UmansExtractor(api_key="k", fallback_model="something-lab")

    def test_permitted_model_accepted(self) -> None:
        assert validate_model(DEFAULT_MODEL) == DEFAULT_MODEL


class TestParseModelOutput:
    def test_plain_json(self) -> None:
        candidate = parse_model_output(
            json.dumps(
                {
                    "capacity_per_module_gb": 32,
                    "module_count": 4,
                    "total_gb": 128,
                    "ddr_gen": "DDR4",
                    "speed_mt": 2400,
                    "form_factor": "RDIMM",
                    "rank_org": "2Rx4",
                    "ecc": True,
                    "registered": True,
                }
            )
        )
        assert candidate is not None
        assert candidate.total_gb == 128
        assert candidate.notes == "llm"

    def test_fenced_json_is_accepted(self) -> None:
        candidate = parse_model_output(
            '```json\n{"capacity_per_module_gb": 32, "module_count": 1, '
            '"total_gb": 32}\n```'
        )
        assert candidate is not None
        assert candidate.total_gb == 32

    def test_speed_read_as_capacity_is_discarded(self) -> None:
        """The characteristic model failure. Arithmetic catches it; hope does not."""
        assert (
            parse_model_output(
                json.dumps(
                    {"capacity_per_module_gb": 3200, "module_count": 1, "total_gb": 3200}
                )
            )
            is None
        )

    def test_inconsistent_arithmetic_is_discarded(self) -> None:
        assert (
            parse_model_output(
                json.dumps(
                    {"capacity_per_module_gb": 32, "module_count": 4, "total_gb": 64}
                )
            )
            is None
        )

    def test_unparseable_output_is_discarded(self) -> None:
        assert parse_model_output("I think this is about 32GB of memory?") is None

    def test_declining_to_commit_is_not_a_spec(self) -> None:
        """The prompt permits null capacity for a self-contradicting title. That is
        a useful answer, but not a specced one."""
        assert parse_model_output(json.dumps({"total_gb": None})) is None


class TestExtractorTransport:
    def test_retries_then_succeeds(self, fake_umans: FakeUmans) -> None:
        """umans fails transiently at any tier. A failure is a reason to try again,
        never a reason to blacklist a model."""
        good = json.dumps(
            {"capacity_per_module_gb": 32, "module_count": 1, "total_gb": 32}
        )
        fake_umans.replies = [None, good]
        url = fake_umans.start()

        with UmansExtractor(api_key="k", base_url=url, max_attempts=2) as extractor:
            candidate = extractor.extract("32GB something unparseable by regex")

        assert candidate is not None
        assert fake_umans.calls >= 2

    def test_total_failure_returns_none_rather_than_raising(
        self, fake_umans: FakeUmans
    ) -> None:
        fake_umans.replies = [None]
        url = fake_umans.start()
        with UmansExtractor(
            api_key="k", base_url=url, max_attempts=1, fallback_model=None
        ) as extractor:
            assert extractor.extract("anything") is None


class TestExtractionRun:
    def add_listing(self, session: Session, item_id: str, title: str) -> None:
        session.add(
            Listing(
                item_id=item_id,
                title=title,
                title_hash=title_hash(title),
                condition_id="3000",
            )
        )
        session.flush()

    def test_confident_regex_titles_need_no_model(self, session: Session) -> None:
        self.add_listing(
            session, "v1|1|0", "32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory"
        )
        run = run_extraction(session, extractor=None)

        assert run.by_regex == 1
        assert run.unresolved == 0
        spec = session.scalars(select(ItemSpec)).one()
        assert spec.method is ExtractionMethod.REGEX
        assert spec.total_gb == 32

    def test_weak_titles_are_left_pending_when_no_model_is_configured(
        self, session: Session
    ) -> None:
        """Correct degraded behavior, not an error — a later pass picks them up."""
        self.add_listing(session, "v1|2|0", "Server memory, see photos")
        run = run_extraction(session, extractor=None)

        assert run.by_regex == 0
        assert run.unresolved == 1
        assert session.scalars(select(ItemSpec)).all() == []

    def test_one_spec_per_distinct_title_not_per_listing(self, session: Session) -> None:
        """Cost control: a shared listing template must be read once."""
        shared = "32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory"
        for n in range(5):
            self.add_listing(session, f"v1|{n}|0", shared)

        assert len(pending_titles(session)) == 1
        run = run_extraction(session, extractor=None)
        assert run.by_regex == 1
        assert len(session.scalars(select(ItemSpec)).all()) == 1

    def test_already_specced_titles_are_not_reconsidered(self, session: Session) -> None:
        self.add_listing(
            session, "v1|3|0", "32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory"
        )
        run_extraction(session, extractor=None)
        second = run_extraction(session, extractor=None)
        assert second.considered == 0

    def test_unreadable_title_stores_nothing_rather_than_a_guess(
        self, session: Session, fake_umans: FakeUmans
    ) -> None:
        """An absent spec is a known gap; a stored bad one is a confident wrong
        answer that goes on to corrupt a cohort."""
        self.add_listing(session, "v1|4|0", "mystery memory lot as pictured")
        fake_umans.replies = ["not json at all"]
        url = fake_umans.start()

        with UmansExtractor(
            api_key="k", base_url=url, max_attempts=1, fallback_model=None
        ) as extractor:
            run = run_extraction(session, extractor=extractor)

        assert run.unresolved == 1
        assert session.scalars(select(ItemSpec)).all() == []


class TestCorrection:
    def test_correction_supersedes_and_is_authoritative(self, session: Session) -> None:
        title = "32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory"
        session.add(
            Listing(
                item_id="v1|9|0", title=title, title_hash=title_hash(title), condition_id="3000"
            )
        )
        session.flush()
        run_extraction(session, extractor=None)

        spec = correct_spec(
            session,
            title_hash(title),
            SpecCandidate(capacity_per_module_gb=64, module_count=2, total_gb=128),
            corrected_by="operator",
        )

        assert spec.method is ExtractionMethod.MANUAL
        assert spec.total_gb == 128
        assert float(spec.confidence or 0) == 1.0
        assert spec.corrected_by == "operator"

    def test_an_implausible_correction_is_refused(self, session: Session) -> None:
        title = "32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory"
        session.add(
            Listing(
                item_id="v1|10|0", title=title, title_hash=title_hash(title), condition_id="3000"
            )
        )
        session.flush()
        run_extraction(session, extractor=None)

        with pytest.raises(ValueError):
            correct_spec(
                session,
                title_hash(title),
                SpecCandidate(capacity_per_module_gb=32, module_count=4, total_gb=999),
                corrected_by="operator",
            )
