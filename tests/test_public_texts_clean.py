"""Предохранитель: в публичных текстах нет формулировок про обход блокировок.

Зачем тест, а не памятка. Партнёр (банк, модерация, платёжный сервис) убирает
проект за любые упоминания обхода блокировок и ограничений доступа — включая
дубли на других языках. Памятку забудут, тест — нет: он падает на сборке.

Что сканируем (только то, что видит клиент):
  * строковые литералы в app/**/*.py — тексты бота, кнопки, публичные страницы;
  * docs/КАНАЛ.md и docs/legal/*.md — заготовки публикаций и документы;
  * brand/source/*.html — тексты, которые попадают на баннеры и обложки;
  * app/web/templates/*.html.

Что НЕ сканируем: докстринги и комментарии (в них правило как раз описано
словами), внутренние исследования docs/УСТОЙЧИВОСТЬ.md и ТЗ — это рабочие
материалы, а не публикация.

Исключение оформляется явно:
    <!-- sanitizer:allow --> ... <!-- /sanitizer:allow -->
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Запрещённые формулировки. Список общий для русского и английского: партнёр
#: убирает проект за любые упоминания обхода блокировок, в том числе в дублях
#: на других языках, поэтому английские шаблоны здесь так же обязательны.
BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    # --- русский ---
    ("обход блокировок", r"обход\w*\s+(блокиров|ограничен|запрет)"),
    ("в обход", r"\bв\s+обход\b"),
    ("обойти блокировку", r"обой(ти|дём|дем|тись)\s+(блокиров|ограничен)"),
    ("цензура", r"цензур"),
    ("DPI", r"\bDPI\b"),
    ("ТСПУ", r"\bТСПУ\b"),
    ("глушилки", r"глушилк"),
    ("белые списки", r"бел(ые|ых|ому|ым)\s+списк"),
    ("LTE", r"\bLTE\b"),
    ("Роскомнадзор/РКН", r"роскомнадзор|\bРКН\b"),
    ("шатдаун", r"шатдаун"),
    ("анонимайзер", r"анонимайзер"),
    ("запрещённые сайты", r"запрещённ\w*\s+(сайт|ресурс|контент)"),
    # --- английский (дубли) ---
    ("bypass", r"\bbypass\w*"),
    ("circumvent", r"\bcircumvent\w*"),
    ("censorship", r"\bcensor\w*"),
    ("DPI (en)", r"\bDPI\b"),
    ("deep packet inspection", r"deep\s+packet"),
    ("throttling", r"\bthrottl\w*"),
    ("unblock sites", r"\bunblock\w*\s+(site|website|resource)"),
    ("shutdown", r"\bshutdown\b"),
    ("anti-censorship", r"anti-censorship"),
)

#: Блоки, которые нужно пропускать: в них формулировки цитируются как «нельзя».
ALLOW_BLOCK = re.compile(r"<!--\s*sanitizer:allow[^>]*-->.*?<!--\s*/sanitizer:allow\s*-->", re.DOTALL)
HTML_TAG = re.compile(r"<[^>]+>")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() узлов-докстрингов: их содержимое — правило для разработчика."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                skip.add(id(body[0].value))
    return skip


def python_public_strings(path: Path) -> list[tuple[int, str]]:
    """Строковые литералы файла без докстрингов: их видит пользователь."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            found.append((getattr(node, "lineno", 0), node.value))
        elif isinstance(node, ast.JoinedStr):  # f-строки: сканируем текстовые куски
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    found.append((getattr(node, "lineno", 0), part.value))
    return found


def text_public_fragments(path: Path) -> list[tuple[int, str]]:
    """Публичный текст файла: без allow-блоков, HTML-комментариев и тегов."""
    raw = path.read_text(encoding="utf-8")
    raw = ALLOW_BLOCK.sub("", raw)
    raw = HTML_COMMENT.sub("", raw)
    stripped = HTML_TAG.sub(" ", raw)
    return [(index + 1, line) for index, line in enumerate(stripped.splitlines()) if line.strip()]


def scan(fragments: list[tuple[int, str]]) -> list[str]:
    """Найти нарушения: «файл:строка: что нашли»."""
    hits: list[str] = []
    for lineno, text in fragments:
        for title, pattern in BANNED_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                hits.append(f"строка {lineno}: «{match.group(0)}» — {title}")
    return hits


def public_python_files() -> list[Path]:
    return sorted(path for path in (ROOT / "app").rglob("*.py") if "__pycache__" not in path.parts)


def public_text_files() -> list[Path]:
    # Условия акций добавляем сюда намеренно: они публикуются в канале, значит
    # обязаны проходить ту же проверку, что описание бота и документы для банка.
    # Иначе текст акции «протухает» незамеченным.
    files = [
        ROOT / "docs" / "КАНАЛ.md",
        ROOT / "docs" / "КОНКУРС-УСЛОВИЯ.md",
        ROOT / "docs" / "СТОРИ.md",
        ROOT / "README.md",
    ]
    legal_dir = ROOT / "docs" / "legal"
    if legal_dir.exists():
        files.extend(sorted(legal_dir.glob("*.md")))
    files.extend(sorted((ROOT / "brand" / "source").glob("*.html")))
    files.extend(sorted((ROOT / "app" / "web" / "templates").glob("*.html")))
    return [path for path in files if path.exists()]


@pytest.mark.parametrize("path", public_python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_python_texts_are_clean(path: Path) -> None:
    """Тексты бота и публичных страниц не упоминают обход блокировок."""
    hits = scan(python_public_strings(path))
    assert not hits, f"{path.relative_to(ROOT)}: {hits}"


@pytest.mark.parametrize("path", public_text_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_public_documents_are_clean(path: Path) -> None:
    """Заготовки публикаций, документы и баннеры не упоминают обход блокировок."""
    hits = scan(text_public_fragments(path))
    assert not hits, f"{path.relative_to(ROOT)}: {hits}"


def test_scanner_actually_catches_phrases() -> None:
    """Проверка самого сканера: он не «зелёный по умолчанию».

    Без этого теста опечатка в регулярном выражении сделала бы все проверки
    выше бессмысленными — они бы всегда проходили.
    """
    samples = [
        "Обход блокировок — наша specialty",
        "работает в обход ограничений оператора",
        "устойчиво к DPI и ТСПУ",
        "bypass censorship and throttling",
    ]
    for sample in samples:
        assert scan([(1, sample)]), f"сканер пропустил: {sample}"


def test_allow_block_is_respected(tmp_path: Path) -> None:
    """Цитаты в памятке «чего писать нельзя» не считаются нарушением."""
    path = tmp_path / "memo.md"
    path.write_text(
        "<!-- sanitizer:allow -->\n| «Обход блокировок» | «Защищённое подключение» |\n<!-- /sanitizer:allow -->\n",
        encoding="utf-8",
    )
    assert not scan(text_public_fragments(path))
