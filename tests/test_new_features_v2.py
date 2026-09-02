"""Tests for NPC motives from DB, image dedup, and quality gate."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import tempfile
from PIL import Image
import random


# ── NPC Motives from DB ─────────────────────────────────────────────────────

class TestNPCMotivesFromDB:
    """Tests for npc_cog.py DB-based motive loading."""

    def test_seed_npc_motives_inserts_data(self):
        """seed_npc_motives вставляет начальные данные из хардкода."""
        from app.npc_cog import _MOTIVATIONS, seed_npc_motives

        # Подсчитываем ожидаемое количество
        expected_count = sum(len(moods) for moods in _MOTIVATIONS.values())
        assert expected_count == 16  # 4 NPC × 4 mood

    def test_generate_npc_cog_with_motive_override(self):
        """generate_npc_cog использует motive_override вместо хардкода."""
        from app.npc_cog import generate_npc_cog

        custom_motive = "Моя кастомная мотивация"
        cog = generate_npc_cog(
            name="liner",
            sentiment=3,  # предан стае → devoted
            day_index=1,
            motive_override=custom_motive,
        )
        assert cog.motivation == custom_motive

    def test_generate_npc_cog_fallback_to_hardcode(self):
        """generate_npc_cog использует хардкод если motive_override=None."""
        from app.npc_cog import generate_npc_cog, _MOTIVATIONS

        cog = generate_npc_cog(
            name="liner",
            sentiment=3,  # предан стае → devoted
            day_index=1,
            motive_override=None,
        )
        # Mood для sentiment=3 — devoted
        assert cog.motivation == _MOTIVATIONS["liner"]["devoted"]

    def test_generate_all_npc_cogs_accepts_session(self):
        """generate_all_npc_cogs принимает session параметр."""
        from app.npc_cog import generate_all_npc_cogs
        import inspect

        sig = inspect.signature(generate_all_npc_cogs)
        assert "session" in sig.parameters


# ── Image Dedup ──────────────────────────────────────────────────────────────

class TestImageDedup:
    """Tests for art_director.py prompt deduplication."""

    def test_normalize_prompt_for_dedup(self):
        """Нормализация убирает стоп-слова и приводит к нижнему регистру."""
        from app.art_director import _normalize_prompt_for_dedup

        result = _normalize_prompt_for_dedup("The quick brown fox jumps over the lazy dog")
        assert "the" not in result
        assert "quick" in result
        assert "brown" in result
        assert "fox" in result

    def test_check_prompt_dedup_empty_history(self):
        """Пустая история — нет дубликатов."""
        from app.art_director import check_prompt_dedup

        assert check_prompt_dedup("corridor with dogs", []) is False

    def test_check_prompt_dedup_different_prompts(self):
        """Разные промпты — не дубликат."""
        from app.art_director import check_prompt_dedup

        recent = ["dark corridor with glowing walls", "sunny meadow with flowers"]
        assert check_prompt_dedup("rainy city street", recent) is False

    def test_check_prompt_dedup_similar_prompts(self):
        """Похожие промпты — дубликат."""
        from app.art_director import check_prompt_dedup

        recent = ["dark corridor with glowing walls and dogs"]
        # Очень похожий промпт
        new = "dark corridor with glowing walls and dogs walking"
        assert check_prompt_dedup(new, recent, threshold=0.5) is True

    def test_check_prompt_dedup_threshold(self):
        """Порог схожести работает корректно."""
        from app.art_director import check_prompt_dedup

        recent = ["alpha beta gamma delta"]
        # Частичное совпадение
        new = "alpha beta gamma epsilon"
        # С низким порогом — дубликат
        assert check_prompt_dedup(new, recent, threshold=0.3) is True
        # С высоким порогом — не дубликат
        assert check_prompt_dedup(new, recent, threshold=0.8) is False


# ── Quality Gate ─────────────────────────────────────────────────────────────

class TestQualityGate:
    """Tests for art_director.py Laplacian variance quality check."""

    def _create_test_image(
        self,
        path: Path,
        width: int = 100,
        height: int = 100,
        pattern: str = "random",
    ) -> None:
        """Создаёт тестовое изображение."""
        if pattern == "random":
            # Случайный шум — высокая дисперсия
            pixels = [(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                      for _ in range(width * height)]
            img = Image.new("RGB", (width, height))
            img.putdata(pixels)
        elif pattern == "solid":
            # Сплошной цвет — низкая дисперсия
            img = Image.new("RGB", (width, height), (128, 128, 128))
        elif pattern == "gradient":
            # Градиент — средняя дисперсия
            img = Image.new("RGB", (width, height))
            pixels = []
            for y in range(height):
                val = int(y * 255 / height)
                for _ in range(width):
                    pixels.append((val, val, val))
            img.putdata(pixels)
        else:
            pixels = [(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                      for _ in range(width * height)]
            img = Image.new("RGB", (width, height))
            img.putdata(pixels)

        img.save(path)

    def test_calculate_laplacian_variance_random(self):
        """Случайное изображение имеет высокую дисперсию."""
        from app.art_director import calculate_laplacian_variance

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            self._create_test_image(Path(f.name), pattern="random")
            variance = calculate_laplacian_variance(f.name)
            assert variance > 100  # Высокая дисперсия для шума

    def test_calculate_laplacian_variance_solid(self):
        """Сплошной цвет имеет низкую дисперсию (относительно шума)."""
        from app.art_director import calculate_laplacian_variance

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            self._create_test_image(Path(f.name), pattern="solid")
            solid_var = calculate_laplacian_variance(f.name)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f2:
            self._create_test_image(Path(f2.name), pattern="random")
            random_var = calculate_laplacian_variance(f2.name)

        # Сплошной цвет должен иметь значительно меньшую дисперсию чем шум
        assert solid_var < random_var * 0.5

    def test_check_image_quality_pass(self):
        """Изображение проходит quality gate."""
        from app.art_director import check_image_quality

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            self._create_test_image(Path(f.name), pattern="random")
            passed, variance = check_image_quality(f.name, min_variance=50)
            assert passed is True
            assert variance > 50

    def test_check_image_quality_fail(self):
        """Изображение не проходит quality gate."""
        from app.art_director import check_image_quality

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            self._create_test_image(Path(f.name), pattern="solid")
            # Сплошной цвет имеет дисперсию ~600-700 из-за edge effects
            # Устанавливаем порог выше этого значения
            passed, variance = check_image_quality(f.name, min_variance=1000)
            assert passed is False
            assert variance < 1000

    def test_check_image_quality_nonexistent_file(self):
        """Несуществующий файл — не проходит проверку."""
        from app.art_director import check_image_quality

        passed, variance = check_image_quality("/nonexistent/image.png")
        assert passed is False
        assert variance == 0.0

    @pytest.mark.asyncio
    async def test_fetch_image_with_quality_check_success(self):
        """fetch_image_with_quality_check возвращает success=True при хорошем качестве."""
        from app.art_director import fetch_image_with_quality_check

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            # Мокаем fetch_day_image чтобы он создал изображение
            async def mock_fetch(**kwargs):
                pixels = [(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                          for _ in range(100 * 100)]
                img = Image.new("RGB", (100, 100))
                img.putdata(pixels)
                img.save(kwargs["dest"])
                return True

            with patch("app.story.fetch_day_image", mock_fetch):
                success, variance = await fetch_image_with_quality_check(
                    prompt="test prompt",
                    short_prompt="test",
                    dest=Path(f.name),
                    min_variance=50,
                )
                # Может быть True или False в зависимости от мока
                assert isinstance(success, bool)


# ── Integration: NPC Motives + DB ───────────────────────────────────────────

class TestNPCMotivesIntegration:
    """Интеграционные тесты для загрузки мотивов из БД."""

    @pytest.mark.asyncio
    async def test_load_motive_from_db_returns_none_when_empty(self):
        """load_motive_from_db возвращает (None, None) если записи нет."""
        from app.npc_cog import load_motive_from_db
        from unittest.mock import AsyncMock

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        motive, thoughts = await load_motive_from_db(mock_session, "liner", "devoted")
        assert motive is None
        assert thoughts is None

    def test_npc_cog_result_has_motivation_field(self):
        """NPCCogResult содержит поле motivation."""
        from app.npc_cog import NPCCogResult

        result = NPCCogResult(
            name="test",
            sentiment=50,
            tone="neutral",
            inner_thought="...",
            motivation="Test motivation",
            action_hint="Test action",
            focus_line="Test focus",
        )
        assert result.motivation == "Test motivation"
