from ssc_control.names import FORMER, People, Person, person


def test_a_person_without_a_name_is_shown_by_email() -> None:
    assert person("  ", "a@example.com", "active") == Person("a@example.com", "a@example.com")
    assert person(None, "a@example.com", "active") == Person("a@example.com", "a@example.com")
    assert person("Ada", "a@example.com", "active") == Person("Ada", "a@example.com")


def test_a_deactivated_or_missing_person_is_a_former_member() -> None:
    assert person("Ada", "a@example.com", "deactivated") == FORMER
    assert FORMER == Person("Former member", None)
    found = People({"usr_a": Person("Ada", "a@example.com")})
    assert (found.name("usr_a"), found.email("usr_a")) == ("Ada", "a@example.com")
    assert (found.name("usr_gone"), found.email("usr_gone")) == ("Former member", None)
    assert (found.name(None), found.email(None)) == (None, None)
