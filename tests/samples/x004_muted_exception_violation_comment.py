"""Sample file demonstrating X004 muted exception handler violation with comment."""
import json
from pathlib import Path
from typing import Any


def migrate(record, manifest_path) -> dict[str, Any]:
    """
    Ensure the title in manifest.json for character visual entries matches the
    descriptive suffix extracted from their filename if there are multiple visuals.
    """
    data = record.data
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        docs = manifest.get("documents", [])

        # Helper to clean and format suffix
        def format_suffix(filename):
            parts = filename.split(".")
            if len(parts) >= 3:
                suffix = parts[-2]
                return suffix.replace("_", " ").title()
            return ""

        # Identify if this specific record needs a suffix update in the manifest
        # 1. Is there a group for this character?
        stem = Path(record.meta.relative_path).stem
        base_name = stem.split("_v")[0] if "_v" in stem else stem

        # Count records with this base_name in manifest
        def _doc_base_name(doc) -> str:
            doc_stem = Path(doc.get("filename", "")).stem
            return doc_stem.split("_v")[0] if "_v" in doc_stem else doc_stem

        group_docs = [
            doc
            for doc in docs
            if doc.get("filename", "").startswith("anchors/characters/") and _doc_base_name(doc) == base_name
        ]

        if len(group_docs) > 1:
            changed = False
            for doc in group_docs:
                filename = doc.get("filename", "")
                suffix = format_suffix(filename)
                if suffix:
                    current_title = doc.get("title", "")
                    if not current_title.endswith(suffix):
                        doc["title"] = f"{current_title} {suffix}"
                        changed = True

            if changed:
                manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Best-effort manifest title touch-up; never let it block the record migration.
        pass

    return data
