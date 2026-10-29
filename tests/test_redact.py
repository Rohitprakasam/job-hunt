import pytest

from agent.redact import RedactionLeak, Redactor


@pytest.fixture
def r(profile, settings):
    return Redactor.from_profile(profile, settings.redaction)


@pytest.mark.parametrize(
    "variant", ["Testa Candidato Q", "TESTA CANDIDATO Q", "testa candidato", "CANDIDATO Q"]
)
def test_name_variants(r, variant):
    out = r.redact(f"Hello {variant}, welcome.")
    assert "[[CANDIDATE_NAME]]" in out
    assert not r.find_leaks(out)


def test_first_and_last_name_alone(r):
    out = r.redact("Dear Testa. Regards, Candidato")
    assert out == "Dear [[CANDIDATE_FIRST_NAME]]. Regards, [[CANDIDATE_LAST_NAME]]"


def test_single_letter_initial_untouched(r):
    assert r.redact("Q: what is a Q value?") == "Q: what is a Q value?"


@pytest.mark.parametrize(
    "phone", ["+91 90000 00001", "09000000001", "90000-00001", "(900) 000-0001", "919000000001"]
)
def test_phone_formats(r, phone):
    out = r.redact(f"Call me at {phone} today")
    assert "[[CANDIDATE_PHONE]]" in out
    assert not r.find_leaks(out)


def test_email(r):
    assert r.redact("mail TESTA.candidato@example.com now") == "mail [[CANDIDATE_EMAIL]] now"


@pytest.mark.parametrize(
    "link",
    [
        "https://linkedin.com/in/testa-candidato-q-000000001/",
        "linkedin.com/in/testa-candidato-q-000000001",
        "https://github.com/TestaCandidato",
        "github.com/testacandidato",
    ],
)
def test_links(r, link):
    out = r.redact(f"see {link} for more")
    assert "[[CANDIDATE_LINK_" in out
    assert not r.find_leaks(out)


def test_handles_alone(r):
    out = r.redact("my handle is TestaCandidato and testa__fake")
    assert out == "my handle is [[CANDIDATE_HANDLE_2]] and [[CANDIDATE_HANDLE_3]]"


def test_other_people_redacted_not_restored(r):
    text = "Recruiter: jane@acme.com, +44 20 7946 0958"
    out = r.redact(text)
    assert out == "Recruiter: [[OTHER_EMAIL]], [[OTHER_PHONE]]"
    assert r.unredact(out) == out


def test_short_numbers_and_dates_kept(r):
    text = "Worked 2022-2024, 99.9% uptime, ticket 12345"
    assert r.redact(text) == text


def test_roundtrip_restores_candidate_values(r):
    text = "Testa Candidato Q <testa.candidato@example.com> +919000000001 https://github.com/TestaCandidato"
    assert r.unredact(r.redact(text)) == text


def test_assert_clean_raises_without_values(r):
    with pytest.raises(RedactionLeak) as err:
        r.assert_clean('{"content": "contact testa.candidato@example.com or 9000000001"}')
    assert "example.com" not in str(err.value)
    assert "email" in str(err.value) and "phone" in str(err.value)


def test_assert_clean_passes_after_redact(r, profile):
    r.assert_clean(r.redact(profile.as_text()))
