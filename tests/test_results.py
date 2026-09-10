from pathlib import Path

import pytest

from zapret_client.results import ReportParser, Result, parse_target


EXAMPLES = Path(__file__).parent / "fixtures/user-examples.txt"


def test_user_examples():
    parser = ReportParser.parse(EXAMPLES.read_text())
    good = parser.results["general (alt).bat"]
    bad = parser.results["general (alt3).bat"]
    assert (good.passed, good.total, good.ping_samples) == (16, 16, 15)
    assert good.average_ping == pytest.approx(64.0)
    assert len(good.warnings) == 2
    assert (bad.passed, bad.total, bad.ping_samples) == (12, 17, 17)
    assert bad.average_ping == pytest.approx(1027 / 17)
    assert not bad.targets["CloudflareWeb"].ok
    assert good.targets["DiscordMain"].ok


@pytest.mark.parametrize("line, expected", [
    ("DNS Ping: Timeout", False),
    ("DNS Ping: 0 ms", True),
    ("DNS Ping: <1 ms", True),
    ("Web HTTP:OK TLS1.2:OK TLS1.3:OK | Ping: Timeout", True),
    ("Web HTTP:SSL TLS1.2:OK TLS1.3:OK | Ping: 40 ms", False),
    ("Web HTTP:UNSUP TLS1.2:UNSUP TLS1.3:UNSUP | Ping: 40 ms", False),
    ("Web HTTP:OK TLS1.2:OK | Ping: 40 ms", False),
    ("Web HTTP:??? | Ping: 40 ms", False),
])
def test_success_rules(line, expected):
    assert parse_target(line).ok is expected


def test_native_saved_report_and_duplicate_targets():
    parser = ReportParser.parse('''Config: general.bat (Type: standard)
      Discord Main : HTTP:OK TLS1.2:OK TLS1.3:OK | Ping: 50 ms
      DNS :  | Ping: 10,5 ms
      DNS :  | Ping: 12,5 ms
=== ANALYTICS ===
general.bat : HTTP OK: 3, ERR: 0
Config: general (ALT).bat (Type: dpi)
      DNS :  | Ping: 999 ms
''')
    result = parser.results["general.bat"]
    assert (result.passed, result.total, result.average_ping) == (2, 2, 31.25)
    assert len(parser.results) == 1
    assert Result.from_dict(result.to_dict()) == result


def test_partial_live_output_does_not_become_complete():
    parser = ReportParser.parse('''[INFO] Targets loaded: 3
[1/2] general.bat
DNS Ping: 10 ms
''', imported=False)
    result = parser.results["general.bat"]
    assert not result.complete
    assert (result.passed, result.total) == (1, 3)
    assert not parser.finished


def test_start_failure_and_empty_ping():
    parser = ReportParser.parse('''[INFO] Targets loaded: 16
[1/2] general.bat
> Strategy failed to start (winws process not found). Skipping...
[2/2] general (ALT).bat
DNS Ping: Timeout
All tests finished.
Results saved to C:\\example.txt
''', imported=False)
    assert parser.results["general.bat"].launch_failed
    assert parser.results["general.bat"].total == 16
    assert parser.results["general (alt).bat"].average_ping is None
    assert parser.finished and parser.saved
