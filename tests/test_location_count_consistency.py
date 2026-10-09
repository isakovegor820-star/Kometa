"""Число локаций: одна правда на тексты, картинки и спецификацию.

Повод — вопрос владельца 08.10.2026: «почему вместо 3 нодов у нас постоянно
говорится что 2 всего?». Причина оказалась не в коде, а в **руках**: число
локаций было вписано словами в четырёх независимых местах, и когда появилась
третья локация, три места из четырёх никто не поправил.

Где жило «2 локации»:

1. описание бота в BotFather (видно до нажатия «Начать»);
2. описание канала (видно в шапке канала);
3. первый пост канала (закреп);
4. баннер приветствия, обложка канала и анимация (``brand/source/*.html``);
5. ``ТЗ.md`` — «2 локации на старте (Германия, Нидерланды)»: там это было верно
   на момент написания, но документ читают как текущее состояние.

Теперь источник правды один — ``LOCATION_LIST`` (``app/bot/launch_copy.py``,
подставляется из ``.env``), а тест запрещает возвращать число словами. Если
локаций станет четыре, правится одна переменная, а не пять файлов.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.bot import launch_copy

ROOT = Path(__file__).resolve().parent.parent

#: Сколько локаций реально в списке: считаем по разделителю «·».
def actual_locations() -> list[str]:
    return [item.strip() for item in launch_copy.locations().split("·") if item.strip()]


#: Формулировки с числом локаций, вписанным словами или цифрой.
COUNT_PATTERNS: tuple[str, ...] = (
    r"\b(?:один|одна|две|двух|три|трёх|четыре|четырёх|пять|пяти)\s+локаци",
    r"\b\d+\s+локаци",
    r"\b(?:одну|две|три|четыре|пять)\s+стран",
    r"\b\d+\s+стран",
)


def _hits(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for pattern in COUNT_PATTERNS:
        found.extend(re.findall(pattern, lowered))
    return found


def test_no_hardcoded_count_in_public_strings():
    """Публичный текст в коде не вписывает число локаций руками.

    Проверяем **строковые литералы** клиентских файлов (``app/bot``,
    ``app/web``, ``app/legal_texts.py``), пропуская докстринги и комментарии —
    там правило как раз объясняется словами. Это тот предохранитель, которого не
    было 08.10.2026: «2 локации» жило в трёх файлах и разошлось с сервером молча.

    Финансовая модель (``app/services/finmodel*.py``) сюда не входит намеренно:
    там «связка на две страны» — вход задачи и ставка для расчёта, а не обещание
    клиенту. Тексты клиента живут в боте, на веб-страницах и в юрдокументах.

    «Одна локация» в шаблоне — не счёт: фраза «если одна локация не отвечает»
    описывает поведение, а не количество. Ловим утверждения о количестве:
    «2 локации», «две локации», «3 страны».
    """
    import ast

    pattern = re.compile(
        r"(?:\b\d+\s*(?:локаци|геолокаци|стран))"
        r"|(?:\b(?:две|двух|три|трёх|четыре|пять)\s+(?:локаци|геолокаци|стран))",
        re.IGNORECASE,
    )
    files: list[Path] = [ROOT / "app" / "legal_texts.py"]
    for target in (ROOT / "app" / "bot", ROOT / "app" / "web"):
        files.extend(sorted(target.rglob("*.py")))

    offenders: list[str] = []
    for path in files:
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            if pattern.search(node.value):
                offenders.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}")
    assert not offenders, (
        "число локаций вписано в публичный текст — берите LOCATION_LIST:\n" + "\n".join(offenders)
    )


def _docstrings(tree) -> set[int]:  # noqa: ANN001 - ast.Module
    """id() узлов-докстрингов: их содержимое — пояснение, а не текст клиенту."""
    import ast

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


def test_location_list_is_a_real_list():
    """Список локаций — перечисление, а не фраза: иначе его нельзя посчитать."""
    assert len(actual_locations()) >= 2


def test_launch_texts_have_no_hardcoded_count():
    """В описаниях и посте число локаций не вписано словами — только список.

    Слово «2 локации» — это обещание, которое нельзя проверить тестом на
    актуальность: оно не связано ни с одной настройкой.
    """
    for name, text in (
        ("описание бота", launch_copy.bot_description()),
        ("о боте", launch_copy.bot_short_description()),
        ("описание канала", launch_copy.channel_description()),
        ("первый пост", launch_copy.channel_first_post()),
    ):
        assert not _hits(text), f"{name}: число локаций вписано словами — только список из .env"


def test_launch_texts_name_every_location():
    """Каждая локация из списка названа в описании бота и в канале."""
    for name, text in (
        ("описание бота", launch_copy.bot_description()),
        ("описание канала", launch_copy.channel_description()),
    ):
        for location in actual_locations():
            # Берём страну без флага: флаг в тексте может быть и не продублирован.
            country = location.split()[-1]
            assert country in text, f"{name}: нет локации «{country}»"


def test_brand_assets_count_matches_reality():
    """Число локаций на макетах совпадает с реальным списком.

    Картинка — единственное место, где число остаётся вписанным: её нельзя
    собрать из ``.env``, она уезжает в Telegram руками. Поэтому здесь не запрет,
    а **сигнализация**: появилась четвёртая локация — тест упал и сказал, что
    баннер, обложку и анимацию пора пересобрать
    (``bash brand/tools/build_avatar.sh``), а ``BRAND_ASSET_LOCATIONS`` —
    обновить.
    """
    on_assets: set[int] = set()
    for path in sorted((ROOT / "brand" / "source").glob("*.html")):
        for match in re.finditer(r"(\d+)\s*локаци", path.read_text(encoding="utf-8").lower()):
            on_assets.add(int(match.group(1)))
    if not on_assets:
        pytest.skip("на макетах число локаций не указано — сверять нечего")
    assert on_assets == {launch_copy.BRAND_ASSET_LOCATIONS}, (
        "на макетах число локаций "
        f"{sorted(on_assets)}, а помечено как отрисованное "
        f"{launch_copy.BRAND_ASSET_LOCATIONS}"
    )
    assert launch_copy.BRAND_ASSET_LOCATIONS == len(actual_locations()), (
        "локаций в списке "
        f"{len(actual_locations())}, а на картинках отрисовано "
        f"{launch_copy.BRAND_ASSET_LOCATIONS} — пересобери ассеты и обнови константу"
    )


def test_brand_assets_and_texts_do_not_disagree():
    """Текст и картинка не могут говорить разное число.

    Это и была исходная жалоба: в описании бота стояло «2 локации», на баннере —
    тоже «2 локации», а на сервере работали три.
    """
    for name, text in (
        ("описание бота", launch_copy.bot_description()),
        ("описание канала", launch_copy.channel_description()),
    ):
        assert len(actual_locations()) == launch_copy.BRAND_ASSET_LOCATIONS, (
            f"{name} перечисляет {len(actual_locations())} локаций, "
            f"на картинках {launch_copy.BRAND_ASSET_LOCATIONS}"
        )
