from __future__ import annotations

from pathlib import Path

import yaml

BUCKETS = ("genres", "moods", "languages", "instruments")


class GenreMapper:
    def __init__(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self._alias = {k.casefold(): v for k, v in (data.get("aliases") or {}).items()}
        self._sets: dict[str, dict[str, str]] = {}
        for bucket in BUCKETS:
            mapping: dict[str, str] = {}
            for item in data.get(bucket) or []:
                mapping[str(item).casefold()] = str(item)
            self._sets[bucket] = mapping

    def classify(self, tags: list[str], extra_language: str | None = None) -> str:
        ordered: dict[str, list[str]] = {b: [] for b in BUCKETS}
        seen: set[str] = set()
        for raw in tags:
            token = self._normalize(raw)
            if not token or token in seen:
                continue
            bucket = self._bucket_for(token)
            if not bucket:
                continue
            label = self._sets[bucket].get(token, token)
            if label.casefold() in {x.casefold() for x in ordered[bucket]}:
                continue
            ordered[bucket].append(self._display(bucket, label))
            seen.add(token)

        if extra_language:
            lang = self._normalize(extra_language)
            lang = self._alias.get(lang, lang)
            if lang:
                display = self._display("languages", self._sets["languages"].get(lang, extra_language))
                existing = {x.casefold() for x in ordered["languages"]}
                if display.casefold() not in existing:
                    ordered["languages"].append(display)

        parts: list[str] = []
        for bucket in BUCKETS:
            parts.extend(ordered[bucket])
        return " | ".join(parts)

    def _normalize(self, raw: str) -> str:
        token = " ".join((raw or "").strip().casefold().split())
        return self._alias.get(token, token)

    def _bucket_for(self, token: str) -> str | None:
        for bucket in BUCKETS:
            if token in self._sets[bucket]:
                return bucket
        return None

    @staticmethod
    def _display(bucket: str, label: str) -> str:
        if bucket == "languages":
            return label[:1].upper() + label[1:] if label else label
        if bucket == "instruments":
            return label[:1].upper() + label[1:] if label else label
        return label
