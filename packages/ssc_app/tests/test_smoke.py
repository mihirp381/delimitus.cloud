from importlib.resources import files


def test_imports():
    import ssc_app  # noqa: F401


def test_the_package_says_it_is_typed() -> None:
    assert files("ssc_app").joinpath("py.typed").is_file()
