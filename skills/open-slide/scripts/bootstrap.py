"""Idempotent runtime bootstrap for open-slide skill."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = SKILL_DIR / "runtime"

PACKAGE_JSON = {
    "name": "open-slide-runtime",
    "private": True,
    "type": "module",
    "scripts": {"dev": "open-slide dev"},
    "dependencies": {
        "@open-slide/core": "^1.2.0",
        "react": "^18.3.1",
        "react-dom": "^18.3.1",
    },
    "devDependencies": {
        "@types/react": "^18.3.12",
        "@types/react-dom": "^18.3.1",
        "vite": "^5.4.10",
    },
}

TSCONFIG_JSON = {
    "compilerOptions": {
        "target": "ES2022",
        "lib": ["ES2022", "DOM", "DOM.Iterable"],
        "module": "ESNext",
        "moduleResolution": "bundler",
        "jsx": "react-jsx",
        "resolveJsonModule": True,
        "isolatedModules": True,
        "moduleDetection": "force",
        "noEmit": True,
        "strict": True,
        "skipLibCheck": True,
        "types": ["@open-slide/core/env"],
    },
    "include": ["open-slide.config.ts"],
}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _write_json(path: Path, data: dict) -> str:
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.write_text(content, encoding="utf-8")
    return content


def _write_config(slides_dir: str) -> None:
    js_path = slides_dir.replace("\\", "/")
    content = (
        "import type { OpenSlideConfig } from '@open-slide/core';\n"
        "\n"
        "const openSlideConfig: OpenSlideConfig = {\n"
        f"  slidesDir: '{js_path}',\n"
        "};\n"
        "\n"
        "export default openSlideConfig;\n"
    )
    (RUNTIME_DIR / "open-slide.config.ts").write_text(content, encoding="utf-8")


def _needs_install(pkg_content: str) -> bool:
    hash_file = RUNTIME_DIR / ".bootstrap-hash"
    current_hash = _md5(pkg_content)
    if not (RUNTIME_DIR / "node_modules").is_dir():
        return True
    if not hash_file.exists():
        return True
    return hash_file.read_text().strip() != current_hash


def _save_hash(pkg_content: str) -> None:
    (RUNTIME_DIR / ".bootstrap-hash").write_text(_md5(pkg_content))


def bootstrap(slides_dir: str | None = None) -> dict:
    npm = shutil.which("npm")
    if not npm:
        return {"status": "error", "message": "npm not found in PATH"}

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    pkg_content = _write_json(RUNTIME_DIR / "package.json", PACKAGE_JSON)

    tsconfig = dict(TSCONFIG_JSON)
    if slides_dir:
        js_path = slides_dir.replace("\\", "/")
        tsconfig["include"] = [f"{js_path}/**/*", "open-slide.config.ts"]
    _write_json(RUNTIME_DIR / "tsconfig.json", tsconfig)

    installed = False
    if _needs_install(pkg_content):
        env = os.environ.copy()
        env["npm_config_cache"] = str(RUNTIME_DIR / ".npm-cache")
        r = subprocess.run(
            [npm, "install"],
            cwd=str(RUNTIME_DIR),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if r.returncode != 0:
            return {
                "status": "error",
                "message": f"npm install failed:\n{r.stderr}",
            }
        _save_hash(pkg_content)
        installed = True

    if slides_dir:
        _write_config(slides_dir)

    return {
        "status": "installed" if installed else "ready",
        "runtime": str(RUNTIME_DIR),
        "slides_dir": slides_dir,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slides-dir", help="Absolute path to slides directory")
    args = parser.parse_args()
    result = bootstrap(args.slides_dir)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["status"] != "error" else 1)
