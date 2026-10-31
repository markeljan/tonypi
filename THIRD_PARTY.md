# Third-party material

| Material | Location / source | Terms and status |
| --- | --- | --- |
| Hiwonder factory action groups | `data/motions/*.json`, converted from `.d6a` | Redistribution permission not established. The MIT license does not cover these files. Resolve with Hiwonder or remove them from all history before public release. |
| Hiwonder controller protocol / logic | `src/tonypi/hal/proto.py`, `src/tonypi/tonyd/body.py` | Protocol implementation and logic mention upstream; provenance and any copied expression need review before release. |
| llama.cpp | Built/downloaded by `provision/provision.sh`; not vendored as a model binary | [MIT](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE). Preserve upstream notices when redistributing. |
| LFM3 models | Downloaded via `provision/models.txt`; not committed | [Liquid model terms](https://huggingface.co/LiquidAI/LFM3-Omni-2B-GGUF); review each model card. |
| Piper voice and sherpa-onnx models | Downloaded via `provision/models.txt` | Review each upstream model's terms; not committed. |
| bruhOS | No identified vendored files in the current tree | Historical provenance requires review; do not assume a license. |

Dependencies installed by package managers retain their own licenses. This inventory is not a grant of rights to third-party material.
