"""Tests for narrative_ai: Entropy, Kolmogorov, SA, GEPA."""

from __future__ import annotations

import math
import zlib
from random import Random

from app.narrative_ai import (
    GEPAPopulation,
    GenerationParams,
    PromptGene,
    coherence_score,
    dynamic_temperature,
    entropy_score,
    entropy_score_from_value,
    is_summary_quality,
    kolmogorov_ratio,
    kolmogorov_score,
    sa_optimize_params,
    should_retry_entropy,
    text_entropy,
)


# ── Shannon Entropy ──

class TestTextEntropy:
    def test_empty(self) -> None:
        assert text_entropy("") == 0.0

    def test_single_char(self) -> None:
        assert text_entropy("a") == 0.0

    def test_repetitive(self) -> None:
        ent = text_entropy("aaa aaa aaa aaa")
        assert ent < 2.0  #很低 — повторы

    def test_diverse(self) -> None:
        text = "Скала дрогнула. Тень упала на броню. Лай разнёс пыль."
        ent = text_entropy(text)
        assert 2.5 < ent < 5.0  # нормальный русский текст


class TestEntropyScore:
    def test_low_entropy(self) -> None:
        score = entropy_score("aaa aaa aaa aaa aaa aaa")
        assert score < 0.7  # низкая энтропия → низкий скор

    def test_target_zone(self) -> None:
        # Генерируем текст с энтропией ~3.8 (целевая зона)
        # Используем повторяющуюся фразу, но с вариациями
        words = ["тень", "лай", "кость", "дым", "ветер", "шрам", "коготь", "перо"]
        text = " ".join(words * 10)
        score = entropy_score(text)
        assert 0.0 <= score <= 1.0


class TestShouldRetryEntropy:
    def test_low_entropy_retry(self) -> None:
        low_ent_text = "день день день день день день день день"
        assert should_retry_entropy(low_ent_text, attempt=1) is True

    def test_high_entropy_no_retry(self) -> None:
        diverse = "Скала дрогнула от удара. Тень упала на броню. Лай разнёс пыль."
        assert should_retry_entropy(diverse, attempt=1) is False

    def test_last_attempt_never_retry(self) -> None:
        low_ent_text = "день день день день день день день день"
        assert should_retry_entropy(low_ent_text, attempt=3, max_attempts=3) is False


class TestDynamicTemperature:
    def test_low_entropy_increases(self) -> None:
        temp = dynamic_temperature(0.85, prev_entropy=2.5)
        assert temp > 0.85

    def test_high_entropy_decreases(self) -> None:
        temp = dynamic_temperature(0.85, prev_entropy=5.0)
        assert temp < 0.85

    def test_normal_entropy_keeps(self) -> None:
        temp = dynamic_temperature(0.85, prev_entropy=3.8)
        assert temp == 0.85


# ── Kolmogorov Complexity ──

class TestKolmogorovRatio:
    def test_empty(self) -> None:
        assert kolmogorov_ratio("") == 0.0

    def test_short_text(self) -> None:
        assert kolmogorov_ratio("abc") == 0.0

    def test_repetitive_low_ratio(self) -> None:
        text = "текст " * 200
        ratio = kolmogorov_ratio(text)
        assert ratio < 0.3  # избыточный текст

    def test_diverse_higher_ratio(self) -> None:
        # Каждое предложение уникально — сжимать нечего
        sentences = [
            "Скала дрогнула отдалённым ударом.",
            "Тень упала на потрескавшуюся броню в коридоре.",
            "Лай разнёс пыль по закоулкам старого портала.",
            "Коготь впился в землю, оставляя глубокую борозду.",
            "Дым поднялся столбом, смешиваясь с звёздной пылью.",
            "Ветер принёс запах гнилой древесины и.remote соли.",
            "Шрам на морде блестел в отблесках костра.",
            "Перья дрейфовали в воздухе, застывая в не自然ных позах.",
        ]
        text = " ".join(sentences)
        ratio = kolmogorov_ratio(text)
        assert ratio > 0.5  # уникальный текст плохо сжимается


class TestKolmogorovScore:
    def test_golden_zone(self) -> None:
        # Уникальный текст, но не хаотичный — золотая зона 0.3-0.75
        text = (
            "Тень скользнула по тёмному коридору. Запах гнилой древесины наполнил комнату. "
            "Кто-то шепчет имя из глубины. Далёкий лай разносится по пустому двору. "
            "Коготь оставил борозду на камне. Дым поднимается столбом к звёздам. "
            "Шрам на морде блестит в отблесках костра. Перо застыло в воздухе."
        )
        score = kolmogorov_score(text)
        assert 0.3 < score <= 1.0  # в золотой зоне или выше

    def test_too_repetitive(self) -> None:
        text = "повтор " * 300
        score = kolmogorov_score(text)
        assert score < 0.5


class TestIsSummaryQuality:
    def test_good_summary(self) -> None:
        text = (
            "Дневник нашёл старую карту в подземелье. Портал дрожит на краю. "
            "Стая решает идти через тёмный коридор. Лай отражается от стен."
        )
        assert is_summary_quality(text) is True

    def test_bad_summary_repetitive(self) -> None:
        text = "день " * 200
        assert is_summary_quality(text) is False


# ── Coherence Score ──

class TestCoherenceScore:
    def test_empty(self) -> None:
        assert coherence_score("") == 0.0

    def test_short_text(self) -> None:
        assert coherence_score("мало текста") == 0.0

    def test_quality_text(self) -> None:
        text = (
            "Скала дрогнула отдалённым ударом. Тень упала на потрескавшуюся броню. "
            "Лай разнёс пыль по закоулкам портала. Коготь впился в землю."
        ) * 5
        score = coherence_score(text)
        assert 0.0 < score <= 1.0


# ── Simulated Annealing ──

class TestSAOptimize:
    def test_basic(self) -> None:
        base = GenerationParams(temperature=0.85, max_tokens=3500)
        texts = ["Тестовый текст для оценки когерентности. " * 20]
        result = sa_optimize_params(base, texts, rounds=3, seed="test")
        assert isinstance(result, GenerationParams)
        assert 0.5 <= result.temperature <= 1.3
        assert 1000 <= result.max_tokens <= 5000

    def test_deterministic(self) -> None:
        base = GenerationParams()
        texts = ["Текст " * 50]
        r1 = sa_optimize_params(base, texts, rounds=5, seed="det")
        r2 = sa_optimize_params(base, texts, rounds=5, seed="det")
        assert r1.temperature == r2.temperature
        assert r1.max_tokens == r2.max_tokens

    def test_empty_texts(self) -> None:
        base = GenerationParams()
        result = sa_optimize_params(base, [], rounds=2, seed="empty")
        assert isinstance(result, GenerationParams)


# ── GEPA ──

class TestPromptGene:
    def test_default(self) -> None:
        g = PromptGene()
        assert g.system_tone == "тёмная сказка"
        assert g.fitness == 0.0

    def test_to_prompt_block(self) -> None:
        g = PromptGene(system_tone="тёмная сказка", pacing_style="быстрый старт")
        block = g.to_prompt_block()
        assert "тёмная сказка" in block or "Пиши как тёмная сказка" in block
        assert "действие" in block or "Начни с действия" in block

    def test_mutation(self) -> None:
        g = PromptGene()
        rng = Random("mutate_test")
        mutated = g.mutate(rng, rate=1.0)  # rate=1.0 — всегда мутируем
        # Хотя бы один параметр должен измениться
        changed = (
            mutated.system_tone != g.system_tone
            or mutated.sensory_emphasis != g.sensory_emphasis
            or mutated.pacing_style != g.pacing_style
        )
        assert changed

    def test_crossover(self) -> None:
        a = PromptGene(system_tone="тёмная сказка")
        b = PromptGene(system_tone="мрачный реализм")
        rng = Random("cross_test")
        child = PromptGene.crossover(a, b, rng)
        assert child.system_tone in ("тёмная сказка", "мрачный реализм")
        assert child.generation == 1

    def test_serialization(self) -> None:
        g = PromptGene(system_tone="лирический нуар", target_length=1400)
        d = g.to_dict()
        g2 = PromptGene.from_dict(d)
        assert g2.system_tone == "лирический нуар"
        assert g2.target_length == 1400


class TestGEPAPopulation:
    def test_default_population(self) -> None:
        pop = GEPAPopulation(seed="test")
        assert len(pop.genes) == 5
        assert pop.generation == 0

    def test_evaluate_fitness(self) -> None:
        pop = GEPAPopulation(seed="test")
        pop.evaluate_fitness(
            week_entropy=3.8,
            week_bigram=0.65,
            week_vote_rate=0.7,
            week_streak_rate=0.6,
        )
        for g in pop.genes:
            assert g.fitness > 0.0

    def test_evolve(self) -> None:
        pop = GEPAPopulation(seed="test")
        pop.evaluate_fitness(3.8, 0.65, 0.7, 0.6)
        old_gen = pop.generation
        new_genes = pop.evolve()
        assert len(new_genes) == 5
        assert pop.generation == old_gen + 1

    def test_best_gene(self) -> None:
        pop = GEPAPopulation(seed="test")
        pop.evaluate_fitness(3.8, 0.65, 0.7, 0.6)
        pop.evolve()
        best = pop.best_gene()
        assert best.fitness >= 0.0

    def test_serialization(self) -> None:
        pop = GEPAPopulation(seed="test")
        pop.evaluate_fitness(3.8, 0.65, 0.7, 0.6)
        pop.evolve()
        json_str = pop.to_json()
        pop2 = GEPAPopulation.from_json(json_str, seed="test")
        assert pop2.generation == pop.generation
        assert len(pop2.genes) == len(pop.genes)

    def test_bad_json_fallback(self) -> None:
        pop = GEPAPopulation.from_json("invalid json {{{", seed="test")
        assert len(pop.genes) == 5  # дефолтная популяция
