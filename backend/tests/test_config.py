from app.core.config import Settings


def test_cors_origins_are_split_and_trimmed() -> None:
    settings = Settings(cors_origins="http://a.com, http://b.com ,")

    assert settings.cors_origin_list == ["http://a.com", "http://b.com"]
