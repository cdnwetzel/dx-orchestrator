from dataclasses import dataclass, field
from enum import Enum
from typing import List


class FitLevel(Enum):
    HIGH = "High"
    PARTIAL = "Partial"
    ANCHORED = "Anchored"


@dataclass
class RoleCard:
    slug: str
    fit: FitLevel
    seat: str
    anchored: bool
    mandate: str = ""
    inputs_required: str = ""
    outputs: str = ""
    operating_checklist: str = ""
    definition_of_done: str = ""
    must_not: str = ""
    failure_modes: str = ""
    handoff: str = ""
    related: str = ""
    prohibited_patterns: List[str] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        return (
            f"# Role: {self.slug} (Fit: {self.fit.value}, Seat: {self.seat})\n"
            f"## Mandate\n{self.mandate}\n\n"
            f"## Must Not (Separation of Duties)\n{self.must_not}\n"
        )
