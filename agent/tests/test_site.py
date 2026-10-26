from pathlib import Path


def test_page_calls_the_public_agent_and_has_a_play_button():
    html = Path(__file__).resolve().parents[2].joinpath("index.html").read_text(encoding="utf-8")
    assert 'lang="es"' in html
    assert ">Play<" in html
    assert "https://mindr-a2a.onrender.com" in html
    assert "/discover" in html
    assert "minuto" in html
    assert "CNAME" not in html
