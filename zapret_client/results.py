"""Parse both console output and the upstream saved standard-test report."""

from dataclasses import asdict, dataclass, field
import re


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HEADER = re.compile(r"^(?:#+\s*)?\[(\d+)/(\d+)\]\s+(general.*?\.bat)\s*$", re.I)
REPORT_HEADER = re.compile(r"^Config:\s*(general.*?\.bat)\s*\(Type:\s*standard\)\s*$", re.I)
TOKEN = re.compile(r"(HTTP|TLS1\.2|TLS1\.3)\s*:\s*([A-Za-z0-9_-]+)", re.I)
PING = re.compile(r"\bPing:\s*(?:(<?\s*\d+(?:[.,]\d+)?)\s*ms|([^|\r\n]+))", re.I)


@dataclass
class Target:
    name: str
    protocols: dict[str, str]
    ping_ms: float | None
    ping_text: str

    @property
    def ok(self) -> bool:
        if self.protocols:
            return all(self.protocols.get(key) == "OK" for key in ("HTTP", "TLS1.2", "TLS1.3"))
        return self.ping_ms is not None

    @property
    def reason(self) -> str:
        if self.protocols:
            return ", ".join(f"{key}: {self.protocols.get(key, 'MISSING')}"
                             for key in ("HTTP", "TLS1.2", "TLS1.3")
                             if self.protocols.get(key) != "OK") or "OK"
        return "OK" if self.ok else f"Ping: {self.ping_text}"


@dataclass
class Result:
    config: str
    targets: dict[str, Target] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    launch_failed: bool = False
    complete: bool = False
    expected: int | None = None

    @property
    def passed(self) -> int:
        return sum(target.ok for target in self.targets.values())

    @property
    def total(self) -> int:
        return max(len(self.targets), self.expected or 0)

    @property
    def average_ping(self) -> float | None:
        values = [t.ping_ms for t in self.targets.values() if t.ping_ms is not None]
        return sum(values) / len(values) if values else None

    @property
    def ping_samples(self) -> int:
        return sum(t.ping_ms is not None for t in self.targets.values())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Result":
        return cls(config=data["config"],
                   targets={name: Target(**target) for name, target in data["targets"].items()},
                   warnings=list(data.get("warnings", [])),
                   launch_failed=bool(data.get("launch_failed", False)),
                   complete=bool(data.get("complete", False)), expected=data.get("expected"))


def clean_line(line: str) -> str:
    return ANSI.sub("", line).lstrip("\ufeff >\t").strip()


def parse_target(line: str) -> Target | None:
    line = clean_line(line)
    ping = PING.search(line)
    if not ping:
        return None
    tokens = list(TOKEN.finditer(line))
    protocol_prefix = re.search(r"\b(?:HTTP|TLS1\.[23])\s*:", line, re.I)
    end = protocol_prefix.start() if protocol_prefix else ping.start()
    name = line[:end].strip(" :|\t")
    if not name or name.startswith(("[", ">", "#")):
        return None
    numeric = ping.group(1)
    value = float(numeric.replace("<", "").strip().replace(",", ".")) if numeric else None
    protocols = {m.group(1).upper(): m.group(2).upper() for m in tokens}
    if protocol_prefix and not protocols:
        protocols = {"HTTP": "MISSING"}
    return Target(name, protocols,
                  value, (ping.group(0).split(":", 1)[1]).strip())


class ReportParser:
    def __init__(self) -> None:
        self.results: dict[str, Result] = {}
        self.current: Result | None = None
        self.index = 0
        self.config_count = 0
        self.expected_targets: int | None = None
        self.finished = False
        self.saved = False
        self.has_errors = False
        self.report_mode = False

    def feed(self, line: str) -> bool:
        """Consume one whole line; return whether visible results changed."""
        line = clean_line(line)
        loaded = re.search(r"Targets loaded:\s*(\d+)", line)
        if loaded:
            self.expected_targets = int(loaded.group(1))
        if "targets.txt missing or empty. Using defaults." in line:
            self.expected_targets = 17
        header = HEADER.match(line)
        report = REPORT_HEADER.match(line)
        if header or report:
            if self.current:
                self.current.complete = True
            if header:
                self.index, self.config_count = int(header[1]), int(header[2])
                name = header[3]
            else:
                self.report_mode = True
                name = report[1]
            self.current = Result(name, expected=self.expected_targets)
            self.results[name.casefold()] = self.current
            return True
        if line.startswith("Config:") or line.startswith("=== ANALYTICS ==="):
            if self.current:
                self.current.complete = True
            self.current = None
        if "All tests finished." in line:
            self.finished = True
            if self.current:
                self.current.complete = True
            self.current = None
        if line.startswith("Results saved to "):
            self.saved = True
        if "[ERROR]" in line:
            self.has_errors = True
        if self.current:
            if "Strategy failed to start" in line:
                self.current.launch_failed = True
                self.current.warnings.append(line)
                return True
            if "[WARN" in line or "[ERROR]" in line:
                self.current.warnings.append(line)
            target = parse_target(line)
            if target:
                self.current.targets[target.name] = target
                return True
        return False

    def finish(self, *, imported: bool = False) -> None:
        # Saved reports have no final console marker. Imported snippets are labeled
        # as imports by the caller, and never become proof of a live completed run.
        if imported and self.current:
            self.current.complete = True

    @classmethod
    def parse(cls, text: str, *, imported: bool = True) -> "ReportParser":
        parser = cls()
        for line in text.splitlines():
            parser.feed(line)
        parser.finish(imported=imported)
        return parser
