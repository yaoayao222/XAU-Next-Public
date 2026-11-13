"""Contract identity must survive Windows checkout newline conversion."""
import pytest

from tooling.contracts.generate import contract_digest


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_contract_digest_ignores_only_newline_encoding(tmp_path, newline):
    paths = [tmp_path / "contracts/openapi.json", tmp_path / "contracts/analysis-answer.schema.json"]
    paths[0].parent.mkdir()
    original = b'{\n  "title": "SYNTHETIC contract",\n  "type": "object"\n}\n'
    for path in paths:
        path.write_bytes(original)
    expected = contract_digest(tmp_path)
    paths[0].write_bytes(original.replace(b"\n", newline))
    assert contract_digest(tmp_path) == expected
    paths[1].write_bytes(original.replace(b"\n", b"\r\n"))
    assert contract_digest(tmp_path) == expected
    paths[1].write_bytes(original.replace(b"object", b"array"))
    assert contract_digest(tmp_path) != expected
