from urllib.parse import parse_qs, urlsplit

import pytest

import drivecheck.access as access_module
from drivecheck.access import AccessLinks
from drivecheck.config import Config


def access_links(tmp_path, origin="https://drivecheck.example.com"):
    return AccessLinks(Config(tmp_path, public_origin=origin))


def token_from(link: str) -> str:
    return parse_qs(urlsplit(link).fragment)["access"][0]


@pytest.mark.parametrize(
    "origin",
    [
        "drivecheck.example.com",
        "ftp://drivecheck.example.com",
        "https://user@drivecheck.example.com",
        "https://user:password@drivecheck.example.com",
        "https://drivecheck.example.com?source=notification",
        "https://drivecheck.example.com#dashboard",
        "https://drivecheck.example.com:invalid",
        "https://drivecheck.example.com/station",
        "https://drivecheck.example.com/path with spaces",
    ],
)
def test_rejects_unsafe_public_origins(tmp_path, origin):
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN"):
        access_links(tmp_path, origin)


def test_missing_public_origin_disables_links(tmp_path):
    links = access_links(tmp_path, "")
    assert links.issue("run-1") == ""
    assert links.redeem("anything") is None


def test_issues_url_encoded_one_time_link(tmp_path):
    links = access_links(tmp_path, "https://drivecheck.example.com/")
    link = links.issue("run /?& ü")
    parsed = urlsplit(link)
    fragment = parse_qs(parsed.fragment)

    assert link.startswith("https://drivecheck.example.com/#access=")
    assert parsed.query == ""
    assert fragment["run"] == ["run /?& ü"]
    token = fragment["access"][0]
    assert links.redeem(token) == "run /?& ü"
    assert links.redeem(token) is None


def test_link_expires_after_ten_minutes(tmp_path, monkeypatch):
    now = 100.0
    monkeypatch.setattr(access_module.time, "monotonic", lambda: now)
    links = access_links(tmp_path)
    token = token_from(links.issue("run-1"))

    now += 599.9
    assert links.redeem(token) == "run-1"

    token = token_from(links.issue("run-2"))
    now += 600
    assert links.redeem(token) is None


def test_keeps_at_most_one_hundred_live_links(tmp_path):
    links = access_links(tmp_path)
    tokens = [token_from(links.issue(f"run-{number}")) for number in range(101)]

    assert len(links._links) == 100
    assert links.redeem(tokens[0]) is None
    assert links.redeem(tokens[-1]) == "run-100"


def test_stores_token_digest_instead_of_raw_token(tmp_path):
    links = access_links(tmp_path)
    token = token_from(links.issue("run-secret"))

    stored = repr(links._links)
    assert token not in stored
    assert AccessLinks._digest(token) in links._links


def test_invalid_tokens_do_not_redeem(tmp_path):
    links = access_links(tmp_path)
    assert links.redeem("") is None
    assert links.redeem("not-an-issued-token") is None
    assert links.redeem(None) is None
