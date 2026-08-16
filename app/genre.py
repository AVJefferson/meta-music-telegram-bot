from __future__ import annotations

from pathlib import Path

import yaml

from app.util import html_esc, unique_names

BUCKETS = ("genres", "moods", "languages", "instruments")


def genre_tokens(value: str) -> list[str]:
    out: list[str] = []
    for chunk in (value or "").replace(",", "|").split("|"):
        token = " ".join(chunk.split()).strip()
        if token:
            out.append(token)
    return out


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
            ordered[bucket].sort(key=str.casefold)
            parts.extend(ordered[bucket])
        return " | ".join(parts)

    def is_allowed(self, raw: str) -> bool:
        token = self._normalize(raw)
        return bool(token) and self._bucket_for(token) is not None

    def compose(self, value: str) -> str:
        allowed: list[str] = []
        unknown: list[str] = []
        for token in unique_names(genre_tokens(value)):
            if self.is_allowed(token):
                allowed.append(token)
            else:
                unknown.append(token)
        unknown.sort(key=str.casefold)
        allowed_text = self.classify(allowed) if allowed else ""
        extra = " | ".join(unknown)
        if allowed_text and extra:
            return f"{allowed_text} | {extra}"
        return allowed_text or extra

    def merge_typed(self, current: str, typed: str) -> str:
        stripped = (typed or "").strip()
        if not stripped:
            return self.compose(current)
        if stripped[0] in "+,|":
            return self.compose(" | ".join(genre_tokens(current) + genre_tokens(stripped[1:])))
        if stripped[0] == "-":
            drop: set[str] = set()
            for raw in genre_tokens(stripped[1:]):
                drop |= self._match_keys(raw)
            kept = [token for token in genre_tokens(current) if not (self._match_keys(token) & drop)]
            return self.compose(" | ".join(kept))
        allowed_raw: list[str] = []
        unknown: list[str] = []
        seen_unknown: set[str] = set()
        for raw in genre_tokens(stripped):
            if self.is_allowed(raw):
                allowed_raw.append(raw)
                continue
            key = raw.casefold()
            if key in seen_unknown:
                continue
            seen_unknown.add(key)
            unknown.append(raw)
        allowed = self.classify(allowed_raw) if allowed_raw else self.classify(genre_tokens(current))
        if unknown:
            extra = " | ".join(unknown)
            merged = f"{allowed} | {extra}" if allowed else extra
            return self.compose(merged)
        return self.compose(allowed)

    def format_html(self, value: str) -> str:
        parts: list[str] = []
        for raw in unique_names(genre_tokens(value)):
            esc = html_esc(raw)
            parts.append(esc if self.is_allowed(raw) else f"<s>{esc}</s>")
        return " | ".join(parts)

    def _match_keys(self, raw: str) -> set[str]:
        keys = {raw.strip().casefold()}
        norm = self._normalize(raw)
        if norm:
            keys.add(norm)
            bucket = self._bucket_for(norm)
            if bucket:
                label = self._sets[bucket].get(norm, raw)
                display = self._display(bucket, label)
                keys.add(label.casefold())
                keys.add(display.casefold())
        return {key for key in keys if key}

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


def diff_genre_html(old: str, new: str, mapper: GenreMapper | None = None) -> str:
    old_tokens = unique_names(genre_tokens(old))
    new_tokens = unique_names(genre_tokens(new))
    new_keys = {token.casefold() for token in new_tokens}
    live: list[str] = []
    for token in new_tokens:
        esc = html_esc(token)
        if mapper is not None and not mapper.is_allowed(token):
            live.append(f"<s>{esc}</s>")
        else:
            live.append(esc)
    removed = [f"<s>{html_esc(token)}</s>" for token in old_tokens if token.casefold() not in new_keys]
    body = " | ".join(live)
    extra = " | ".join(removed)
    if body and extra:
        return f"{body} | {extra}"
    return body or extra or "—"
