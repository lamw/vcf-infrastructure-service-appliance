from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class ValidationResult:
    ok: bool
    message: str
    checked_at: str

    @classmethod
    def pending(cls) -> "ValidationResult":
        return cls(ok=False, message="Not validated yet", checked_at="")


@dataclass
class ServiceDefinition:
    id: str
    name: str
    description: str
    enabled: bool
    configured: bool
    health_status: str
    endpoint: str
    filesystem_root: str
    settings: Dict[str, object]
    last_validation_result: ValidationResult = field(default_factory=ValidationResult.pending)
    last_health_check_time: Optional[str] = None
    quick_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["health"] = data.pop("health_status")
        return data


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
