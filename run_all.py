"""
End-to-end runner: generate -> normalize -> train -> serve.

    python run_all.py            # run pipeline then start the API on :8000
    python run_all.py --no-serve # just build data + model, don't start server
"""
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")


def step(title, module):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)
    r = subprocess.run([sys.executable, os.path.join(SRC, module)], cwd=SRC)
    if r.returncode != 0:
        sys.exit(f"Step failed: {module}")


def main():
    step("STAGE 1/4  Generate per-carrier fare feeds", "generate_fares.py")
    step("STAGE 2/4  Normalise into canonical offers", "normalize.py")
    step("STAGE 3/4  Train LightGBM price-rise model", "train_model.py")

    if "--no-serve" in sys.argv:
        print("\nPipeline complete. Start the API with:  python src/api.py")
        return

    print("\n" + "=" * 64)
    print("STAGE 4/4  Starting FastAPI server -> http://127.0.0.1:8000")
    print("=" * 64)
    import uvicorn
    sys.path.insert(0, SRC)
    import api
    uvicorn.run(api.app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
