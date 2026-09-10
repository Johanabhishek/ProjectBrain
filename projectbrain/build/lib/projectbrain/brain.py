import json
import re
from pathlib import Path

from .parsers import parse_python, parse_js

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", ".turbo", "coverage",
    ".cache", ".parcel-cache", ".webpack", "out", ".output",
    "dist-deploy", ".vite-cache",
}

# Exact filenames that are always bundles/artifacts
SKIP_FILES = {
    "out.js", "out.cjs", "out.mjs", "out.min.js",
    "bundle.js", "bundle.cjs", "bundle.min.js",
    "index.min.js", "main.min.js",
    "vendor.js", "vendors.js", "chunk.js",
}

# Filename patterns treated as minified/generated
SKIP_SUFFIXES = {".min.js", ".min.cjs", ".min.mjs", ".min.ts"}

# If any line in the file exceeds this, treat it as minified
MINIFIED_LINE_THRESHOLD = 500

PYTHON_EXTS = {".py"}
JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "i", "my", "me", "we", "our",
    "in", "on", "at", "to", "for", "of", "and", "or", "but", "not",
    "why", "am", "getting", "get", "how", "what", "when", "where", "which",
    "this", "that", "it", "its", "with", "from", "by", "about", "into",
}


class ProjectBrain:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self._index: dict = {}
        self._blobs: dict[str, str] = {}
        self._cache_path = self.root / ".projectbrain.json"

    def index(self) -> dict:
        """Parse all Python and JS/TS files under root and cache the result."""
        self._index = {}

        for path in self._walk():
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            ext = path.suffix.lower()

            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            if _is_minified(source):
                continue

            info = parse_python(source) if ext in PYTHON_EXTS else parse_js(source)
            info["mtime"] = path.stat().st_mtime
            self._index[rel] = info

        self._cache_path.write_text(
            json.dumps(self._index, indent=2), encoding="utf-8"
        )
        self._build_blobs()
        return self.stats()

    def load(self) -> bool:
        """Load a previously saved index from disk."""
        if self._cache_path.exists():
            self._index = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self._build_blobs()
            return True
        return False

    def _build_blobs(self) -> None:
        """Precompute a lowercase searchable string per file, once per index/load."""
        self._blobs = {}
        for rel_path, info in self._index.items():
            parts = [
                rel_path,
                *[f["name"] for f in info.get("functions", [])],
                *[c["name"] for c in info.get("classes", [])],
                *[r["path"] for r in info.get("routes", [])],
                *info.get("imports", []),
                *info.get("exports", []),
            ]
            self._blobs[rel_path] = " ".join(parts).lower()

    def query(self, prompt: str, top_n: int = 10) -> list[dict]:
        """Return the top_n most relevant files for a natural language prompt."""
        if not self._index:
            self.load()

        keywords = _extract_keywords(prompt)
        if not keywords:
            return []

        scores: dict[str, float] = {}
        for rel_path, blob in self._blobs.items():
            path_lower = rel_path.lower()
            score = 0.0
            for kw in keywords:
                if kw in blob:
                    score += 1.0
                    if kw in path_lower:
                        score += 0.5
            if score > 0:
                scores[rel_path] = score

        ranked = sorted(scores, key=lambda p: scores[p], reverse=True)[:top_n]

        # Reason-building inspects per-file structure (functions/classes/routes/
        # imports/exports) — only worth doing for the handful of files we're
        # actually about to return, not the whole index.
        return [self._build_result(rel_path, scores[rel_path], keywords) for rel_path in ranked]

    def _build_result(self, rel_path: str, score: float, keywords: list[str]) -> dict:
        info = self._index[rel_path]
        path_lower = rel_path.lower()
        reasons: list[str] = []

        for kw in keywords:
            if kw in path_lower:
                reasons.append(f"filename matches '{kw}'")
            for fn in info.get("functions", []):
                if kw in fn["name"].lower():
                    reasons.append(f"defines function '{fn['name']}'")
            for c in info.get("classes", []):
                if kw in c["name"].lower():
                    reasons.append(f"defines class '{c['name']}'")
            for r in info.get("routes", []):
                if kw in r["path"].lower():
                    reasons.append(f"defines route {r['method']} {r['path']}")
            for imp in info.get("imports", []):
                if kw in imp.lower():
                    reasons.append(f"imports '{imp}'")
            for exp in info.get("exports", []):
                if kw in exp.lower():
                    reasons.append(f"exports '{exp}'")

        seen: set[str] = set()
        deduped = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                deduped.append(r)

        return {
            "file_path": str(self.root / rel_path),
            "rel_path": rel_path,
            "score": score,
            "reason": "; ".join(deduped[:3]) if deduped else "keyword match",
            "functions": info.get("functions", []),
            "classes": info.get("classes", []),
            "routes": info.get("routes", []),
            "imports": info.get("imports", []),
            "exports": info.get("exports", []),
            "connections": self._connections(rel_path),
        }

    def _find_importers(self, rel_path: str, limit: int = 5) -> list[str]:
        """Files that import rel_path (1-hop, reverse direction)."""
        stem = Path(rel_path).stem
        importers = [
            other
            for other, info in self._index.items()
            if other != rel_path
            and any(stem in imp for imp in info.get("imports", []))
        ]
        return importers[:limit]

    def _resolve_imports(self, rel_path: str, limit: int = 5) -> list[str]:
        """Files rel_path imports, resolved to index entries (1-hop, forward direction)."""
        imports = self._index.get(rel_path, {}).get("imports", [])
        resolved = [
            other
            for other in self._index
            if other != rel_path
            and any(Path(other).stem in imp for imp in imports)
        ]
        return resolved[:limit]

    def _connections(self, rel_path: str) -> list[dict]:
        """1-hop graph neighbors: what this file imports and who imports it."""
        connections = [
            {"rel_path": other, "relation": "imports"}
            for other in self._resolve_imports(rel_path)
        ]
        connections += [
            {"rel_path": other, "relation": "imported_by"}
            for other in self._find_importers(rel_path)
        ]
        return connections

    def get_context_for_file(self, file_path: str) -> dict:
        """Return parsed info for a file plus its 1-hop import/importer chain."""
        if not self._index:
            self.load()

        target = Path(file_path)
        try:
            rel = str(target.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            rel = file_path.replace("\\", "/")

        if rel not in self._index:
            return {}

        info = self._index[rel]

        return {
            "file_path": file_path,
            "rel_path": rel,
            **info,
            "imported_by": self._find_importers(rel, limit=999),
            "imports_resolved": self._resolve_imports(rel, limit=999),
        }

    def stats(self) -> dict:
        """Summary counts for the current index."""
        return {
            "files": len(self._index),
            "functions": sum(len(v.get("functions", [])) for v in self._index.values()),
            "classes": sum(len(v.get("classes", [])) for v in self._index.values()),
            "routes": sum(len(v.get("routes", [])) for v in self._index.values()),
        }

    def _walk(self):
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in SKIP_DIRS for p in path.parts):
                continue
            # Extension check first: never even open images/binaries/lockfiles/
            # etc. — only files we'd actually parse are worth reading at all.
            ext = path.suffix.lower()
            if ext not in PYTHON_EXTS and ext not in JS_EXTS:
                continue
            if path.name in SKIP_FILES:
                continue
            if any(path.name.endswith(s) for s in SKIP_SUFFIXES):
                continue
            yield path


def _is_minified(source: str) -> bool:
    return any(len(line) > MINIFIED_LINE_THRESHOLD for line in source.splitlines())


def _extract_keywords(prompt: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_/.-]+", prompt.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]
