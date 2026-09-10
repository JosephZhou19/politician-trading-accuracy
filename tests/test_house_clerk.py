from src.ingest.house_clerk import parse_house_name


def test_parse_house_name_strips_hon_title():
    assert parse_house_name("Gottheimer, Hon.. Josh") == ("Josh", "Gottheimer")


def test_parse_house_name_strips_non_hon_titles():
    """Regression: a real filing used "Mrs.." instead of "Hon..", which - unstripped -
    got parsed as the first name, creating a duplicate legislator row for the same
    person (Marjorie Greene / "Mrs.. Greene")."""
    assert parse_house_name("Greene, Mrs.. Marjorie Taylor") == ("Marjorie", "Greene")


def test_parse_house_name_middle_name_dropped():
    assert parse_house_name("Gottheimer, Hon.. Josh Middle") == ("Josh", "Gottheimer")
