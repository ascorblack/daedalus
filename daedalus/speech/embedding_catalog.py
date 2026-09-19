"""Pinned multilingual text models, installed by the shared model download manager."""
from dataclasses import dataclass
from pathlib import Path

REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
BASE = f"https://huggingface.co/intfloat/multilingual-e5-small/resolve/{REVISION}"


@dataclass(frozen=True)
class ModelFile:
    id: str
    archive: str
    url: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class EmbeddingModel:
    id: str = "multilingual-e5-small"
    label: str = "Multilingual E5 Small · int8"
    archive: str = "multilingual-e5-small"
    url: str = BASE
    dimension: int = 384
    licence: str = "MIT"
    languages: tuple[str, ...] = ("ru", "en")
    files: tuple[ModelFile, ...] = (
        ModelFile("multilingual-e5-small", "model.onnx", f"{BASE}/onnx/model_qint8_avx512_vnni.onnx", 118346824,
                  "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"),
        ModelFile("multilingual-e5-small", "tokenizer.json", f"{BASE}/tokenizer.json", 17082730,
                  "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
    )

    @property
    def sha256(self) -> str:
        return self.files[0].sha256

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def unpacked_bytes(self) -> int:
        return self.size_bytes

    @property
    def space(self) -> str:
        return f"{self.id}:{REVISION}:mean:384:passage-text-v2"


MODEL = EmbeddingModel()
MODELS = (MODEL,)


def get(model_id: str) -> EmbeddingModel:
    if model_id != MODEL.id:
        raise KeyError(model_id)
    return MODEL


def resolve(directory: Path, model: EmbeddingModel) -> None:
    for file in model.files:
        if not (directory / file.archive).is_file():
            raise ValueError("the embedding model is incomplete")
