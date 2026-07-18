"""Foreign-script title filter (≥50% non-Latin letters)."""

from app.services.prowlarr import title_is_mostly_foreign_script


def test_all_cjk_rejected():
    assert title_is_mostly_foreign_script("三体")
    assert title_is_mostly_foreign_script("ノルウェイの森")
    assert title_is_mostly_foreign_script("데미안")


def test_all_cyrillic_rejected():
    assert title_is_mostly_foreign_script("Достоевский - Бесы")
    assert title_is_mostly_foreign_script("Война и мир")


def test_majority_foreign_rejected():
    # 4 CJK + 2 Latin letters → 4/6 ≈ 67% foreign
    assert title_is_mostly_foreign_script("三国演义ab")
    # Equal Latin/foreign letters (3/6 = 50%) — prune at the 50% line
    assert title_is_mostly_foreign_script("abc가나다")


def test_minority_foreign_kept():
    # One CJK char among many Latin letters
    assert not title_is_mostly_foreign_script("The Three-Body Problem 三")
    # Mostly Latin with a short Cyrillic word
    assert not title_is_mostly_foreign_script("Dostoevsky Бесы audiobook")
    # Just under half: 2 Hangul / 5 letters = 40%
    assert not title_is_mostly_foreign_script("abc한글")


def test_latin_with_diacritics_kept():
    # Accented Latin is still Latin script — French/Spanish/German stay.
    assert not title_is_mostly_foreign_script("L'Étranger - Albert Camus")
    assert not title_is_mostly_foreign_script("Cien años de soledad")
    assert not title_is_mostly_foreign_script("Der Prozess - Kafka")


def test_english_kept():
    assert not title_is_mostly_foreign_script("Dungeon Crawler Carl - Matt Dinniman [m4b]")
    assert not title_is_mostly_foreign_script("")
    assert not title_is_mostly_foreign_script("12345 [2024]")


def test_punctuation_digits_ignored_in_ratio():
    # "三体" + lots of punctuation/digits doesn't dilute the ratio.
    assert title_is_mostly_foreign_script("三体 (2024) [1080p] [m4b]")
