from __future__ import annotations

import pytest
from pydantic import ValidationError

from daedalus.host.proposals import ProposalJob, new_proposal, same_generation, with_activity


def test_common_proposal_envelope_is_typed_and_activity_is_bounded() -> None:
    value = new_proposal("rules", request="change")
    for index in range(60):
        value = with_activity(value, f"stage-{index}")
    parsed = ProposalJob.model_validate(value)
    assert parsed.domain == "rules" and parsed.state == "planning"
    assert len(parsed.activity) == 40 and parsed.activity[-1].stage == "stage-59"
    assert parsed.model_extra == {"request": "change"}


def test_generation_identity_rejects_late_callbacks() -> None:
    value = new_proposal("rules")
    assert same_generation(value, value)
    assert not same_generation({**value, "generation_id": "new"}, value)
    with pytest.raises(ValidationError):
        ProposalJob.model_validate({**value, "state": "unknown"})
