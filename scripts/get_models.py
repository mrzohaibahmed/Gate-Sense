"""Download and verify the models GateSense uses, so a gate PC can be prepared before it goes offline.

- InsightFace buffalo_s (face detection and recognition): downloaded by InsightFace to
  %USERPROFILE%\\.insightface\\models\\buffalo_s on first use; this loads it once.
- Liveness (anti-spoofing): MiniFASNetV2 and MiniFASNetV1SE, ONNX, to LIVENESS_MODEL_DIR
  (%USERPROFILE%\\.gatesense\\models, or GATESENSE_MODEL_DIR). Each file is checked against the SHA-256 in
  app/config.py LIVENESS_MODELS; a file that doesn't match is downloaded again, and never used.

Licences: Silent-Face-Anti-Spoofing (MiniVision) and its ONNX export (yakhyo/face-anti-spoofing) are Apache-2.0.
InsightFace's pretrained buffalo_s weights are for non-commercial research use (see PLAN.md, Models).

Run from the project root:
    .venv\\Scripts\\python scripts\\get_models.py            # download what's missing, verify everything
    .venv\\Scripts\\python scripts\\get_models.py --check    # verify only, download nothing (exit 1 if a file is bad)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, liveness  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and verify GateSense's models.")
    parser.add_argument("--check", action="store_true", help="verify only, download nothing")
    parser.add_argument("--skip-insightface", action="store_true", help="liveness models only")
    args = parser.parse_args()
    ok = True

    folder = config.LIVENESS_MODEL_DIR
    print(f"Liveness models ({liveness.SOURCE})\n  folder {folder}")
    bad = liveness.problems(folder)
    if bad and not args.check:
        try:
            liveness.download(folder, log=lambda m: print(f"  {m}"))
        except liveness.LivenessUnavailable as e:
            print(f"  ERROR: {e}")
            ok = False
        bad = liveness.problems(folder)
    for name, (_url, digest, scale) in config.LIVENESS_MODELS.items():
        state = bad.get(name, "OK, SHA-256 verified")
        print(f"  {name:<22} crop x{scale:<4} {state}  ({digest[:16]}...)")
    if bad:
        ok = False
    else:
        try:
            model = liveness.Liveness()
            print(f"  loaded: {', '.join(f'{s.get_providers()[0]} {size}x{size}' for s, _, _, size in model.models)}")
        except Exception as e:  # noqa: BLE001  report any loader error plainly
            print(f"  ERROR: verified, but onnxruntime can't load them: {e}")
            ok = False

    if not args.skip_insightface:
        print(f"\nInsightFace {config.MODEL_PACK} (downloads on first use)")
        if args.check:
            pack = Path.home() / ".insightface" / "models" / config.MODEL_PACK
            files = sorted(p.name for p in pack.glob("*.onnx"))
            print(f"  {pack}: {', '.join(files) if files else 'not downloaded yet'}")
            ok = ok and bool(files)
        else:
            from app.models import face_analysis

            fa = face_analysis()
            print(f"  loaded: {sorted(fa.models)} from {fa.model_dir}")

    print("\nModels OK" if ok else "\nSome models are missing or don't match: see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
