import tones


def test_load_falls_back_to_example_when_tones_yaml_absent(monkeypatch, tmp_path):
    # Simulate a fresh clone: no tones.yaml. load() must read tones.example.yaml.
    monkeypatch.setattr(tones, "TONES_FILE", tmp_path / "tones.yaml")
    data = tones.load()
    assert data["tones"], "expected tones from the shipped example file"
    assert "shared_system_prompt" in data
