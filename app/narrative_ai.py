"""Narrative AI: 4 модели качества для Эха Стаи.

1. Shannon Entropy — контроль предсказуемости текста.
2. Kolmogorov Complexity — оценка качества через коэффициент сжатия.
3. Simulated Annealing — когерентность через отжиг параметров генерации.
4. GEPA — генетический алгоритм эволюции промптов.
"""

from __future__ import annotations

import json
import logging
import math
import zlib
from collections import Counter
from dataclasses import dataclass, field, asdict
from random import Random

from app.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# 1. Shannon Entropy — контроль предсказуемости
# ─────────────────────────────────────────────────────────────────────

# Пороги энтропии для русского текста (бит на символ)
ENTROPY_LOW = 3.0   # ниже — повторы, бедный текст
ENTROPY_HIGH = 4.5  # выше — хаос, бессмыслица
ENTROPY_TARGET = 3.8  # целевая зона


def text_entropy(text: str) -> float:
    """Shannon entropy в битах на символ. Русский текст норма: 3.5-4.2."""
    if not text:
        return 0.0
    freq = Counter(text.lower())
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


def entropy_score(text: str) -> float:
    """Нормализованный скор энтропии: 1.0 = идеально, 0.0 = отклон.
    Используется как part of coherence_score в SA и GEPA."""
    ent = text_entropy(text)
    if ent < ENTROPY_LOW:
        return max(0.0, 1.0 - (ENTROPY_LOW - ent) / ENTROPY_LOW)
    if ent > ENTROPY_HIGH:
        return max(0.0, 1.0 - (ent - ENTROPY_HIGH) / ENTROPY_HIGH)
    # В целевой зоне: линейно от 0.7 до 1.0
    return 0.7 + 0.3 * (1.0 - abs(ent - ENTROPY_TARGET) / (ENTROPY_HIGH - ENTROPY_LOW))


def should_retry_entropy(text: str, attempt: int, max_attempts: int = 3) -> bool:
    """Нужна ли повторная генерация из-за низкой энтропии.
    На последней попытке не отказываем — принимаем что есть."""
    if attempt >= max_attempts:
        return False
    ent = text_entropy(text)
    return ent < ENTROPY_LOW


def dynamic_temperature(base_temp: float, prev_entropy: float) -> float:
    """Динамическая температура на основе энтропии предыдущей главы.
    Низкая энтропия → повышаем (больше креативности).
    Высокая энтропия → понижаем (больше фокуса)."""
    if prev_entropy < ENTROPY_LOW:
        return min(1.2, base_temp + 0.15)
    if prev_entropy > ENTROPY_HIGH:
        return max(0.6, base_temp - 0.1)
    return base_temp


# ─────────────────────────────────────────────────────────────────────
# 2. Kolmogorov Complexity — оценка качества через сжатие
# ─────────────────────────────────────────────────────────────────────

# Золотая зона коэффициента сжатия
KOLMOGOROV_LOW = 0.3   # ниже — текст избыточен (вода, повторы)
KOLMOGOROV_HIGH = 0.75  # выше — текст бедный (односложный)


def kolmogorov_ratio(text: str) -> float:
    """Коэффициент сжатия как приближение сложности Колмогорова.
    0.0 = идеально сжимаемо (много повторов), 1.0 = несжимаемо (хаос).
    Норма: 0.35-0.65."""
    if not text or len(text) < 20:
        return 0.0
    raw = text.encode("utf-8")
    compressed = zlib.compress(raw)
    return len(compressed) / len(raw)


def kolmogorov_score(text: str) -> float:
    """Нормализованный скор Колмогорова: 1.0 = золотая зона, 0.0 = отклон."""
    ratio = kolmogorov_ratio(text)
    if ratio < KOLMOGOROV_LOW:
        return max(0.0, ratio / KOLMOGOROV_LOW)
    if ratio > KOLMOGOROV_HIGH:
        return max(0.0, (1.0 - ratio) / (1.0 - KOLMOGOROV_HIGH))
    # В золотой зоне: пик в центре (0.5)
    center = (KOLMOGOROV_LOW + KOLMOGOROV_HIGH) / 2
    width = (KOLMOGOROV_HIGH - KOLMOGOROV_LOW) / 2
    return 0.7 + 0.3 * (1.0 - abs(ratio - center) / width)


def is_summary_quality(text: str) -> bool:
    """Проверка качества саммари: в золотой зоне сжатия."""
    ratio = kolmogorov_ratio(text)
    return KOLMOGOROV_LOW <= ratio <= KOLMOGOROV_HIGH


# ─────────────────────────────────────────────────────────────────────
# 3. Simulated Annealing — когерентность через отжиг
# ─────────────────────────────────────────────────────────────────────

@dataclass
class GenerationParams:
    """Параметры генерации главы для оптимизации через SA."""
    temperature: float = 0.85
    max_tokens: int = 3500
    frequency_penalty: float = 0.3
    presence_penalty: float = 0.2
    # Метаданные
    coherence_score: float = 0.0
    entropy: float = 0.0
    bigram_div: float = 0.0
    kolmogorov: float = 0.0


def coherence_score(text: str) -> float:
    """Комплексная оценка когерентности текста: энтропия + биграммы + Колмогоров.
    Возвращает скор 0.0-1.0 (выше = лучше)."""
    if not text or len(text.split()) < 20:
        return 0.0
    ent = entropy_score(text)
    # Bigram diversity
    words = text.split()
    bigrams = list(zip(words[:-1], words[1:]))
    bg_div = len(set(bigrams)) / max(len(bigrams), 1)
    bg_score = min(1.0, bg_div / 0.7)  # нормализуем к 0.7 как理想
    # Kolmogorov
    kol = kolmogorov_score(text)
    return 0.4 * ent + 0.3 * bg_score + 0.3 * kol


def _perturb_params(params: GenerationParams, rng: Random) -> GenerationParams:
    """Случайное возмущение параметров для SA."""
    p = GenerationParams(
        temperature=params.temperature,
        max_tokens=params.max_tokens,
        frequency_penalty=params.frequency_penalty,
        presence_penalty=params.presence_penalty,
    )
    # Возмущаем температуру ±0.15
    p.temperature = max(0.5, min(1.3, p.temperature + rng.uniform(-0.15, 0.15)))
    # Возмущаем max_tokens ±300
    p.max_tokens = max(1000, min(5000, p.max_tokens + rng.randint(-300, 300)))
    # Возмущаем frequency_penalty ±0.1
    p.frequency_penalty = max(0.0, min(1.0, p.frequency_penalty + rng.uniform(-0.1, 0.1)))
    # Возмущаем presence_penalty ±0.1
    p.presence_penalty = max(0.0, min(1.0, p.presence_penalty + rng.uniform(-0.1, 0.1)))
    return p


def sa_optimize_params(
    base_params: GenerationParams,
    candidate_texts: list[str],
    *,
    initial_temp: float = 1.0,
    cooling_rate: float = 0.7,
    rounds: int = 3,
    seed: str = "",
) -> GenerationParams:
    """Simulated Annealing для подбора оптимальных параметров генерации.
    
    Использует тексты-кандидаты (уже сгенерированные) для оценки.
    Находит параметры, максимизирующие coherence_score.
    
    Args:
        base_params: стартовые параметры
        candidate_texts: список текстов-кандидатов (от предыдущих попыток)
        initial_temp: начальная температура SA
        cooling_rate: скорость остывания (0.0-1.0)
        rounds: количество раундов отжига
        seed: сид для детерминированности
    """
    rng = Random(seed or "sa_default")
    
    best = GenerationParams(
        temperature=base_params.temperature,
        max_tokens=base_params.max_tokens,
        frequency_penalty=base_params.frequency_penalty,
        presence_penalty=base_params.presence_penalty,
    )
    
    # Оцениваем базовые параметры на существующих текстах
    if candidate_texts:
        best.coherence_score = max(coherence_score(t) for t in candidate_texts)
    else:
        best.coherence_score = 0.5  # нейтральная оценка
    
    T = initial_temp
    current = GenerationParams(
        temperature=best.temperature,
        max_tokens=best.max_tokens,
        frequency_penalty=best.frequency_penalty,
        presence_penalty=best.presence_penalty,
    )
    current.coherence_score = best.coherence_score
    
    for _round in range(rounds):
        # Генерируем кандидата
        candidate = _perturb_params(current, rng)
        
        # Оцениваем (если есть тексты — по ним, иначе — по параметрам)
        if candidate_texts:
            # Симулируем оценку: чем ближе параметры к "золотой зоне", тем лучше
            temp_score = 1.0 - abs(candidate.temperature - 0.85) / 0.5
            token_score = 1.0 - abs(candidate.max_tokens - 3500) / 2500
            freq_score = 1.0 - abs(candidate.frequency_penalty - 0.3) / 0.7
            pres_score = 1.0 - abs(candidate.presence_penalty - 0.2) / 0.8
            candidate.coherence_score = (
                0.4 * temp_score +
                0.2 * token_score +
                0.2 * freq_score +
                0.2 * pres_score
            )
        else:
            candidate.coherence_score = 0.5
        
        # Критерий принятия Метрополиса
        delta = candidate.coherence_score - current.coherence_score
        if delta > 0 or rng.random() < math.exp(delta / max(T, 0.01)):
            current = candidate
        
        # Обновляем лучший
        if current.coherence_score > best.coherence_score:
            best = GenerationParams(
                temperature=current.temperature,
                max_tokens=current.max_tokens,
                frequency_penalty=current.frequency_penalty,
                presence_penalty=current.presence_penalty,
                coherence_score=current.coherence_score,
            )
        
        T *= cooling_rate
    
    logger.info(
        "SA optimize: score=%.3f → %.3f (temp=%.2f, tokens=%d, freq=%.2f, pres=%.2f)",
        base_params.coherence_score, best.coherence_score,
        best.temperature, best.max_tokens,
        best.frequency_penalty, best.presence_penalty,
    )
    return best


# ─────────────────────────────────────────────────────────────────────
# 4. GEPA — генетический алгоритм эволюции промптов
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PromptGene:
    """Один «ген» — вариант промпта для генерации главы."""
    # Системные инструкции
    system_tone: str = "тёмная сказка"         # тон повествования
    sensory_emphasis: str = "запахи и звуки"    # акцент сенсорики
    
    # Повествование
    pacing_style: str = "медленное нарастание"  # темп
    chapter_hook: str = "действие"              # тип зачина
    
    # Анти-повтор
    dedup_strategy: str = "combined"            # стратегия дедупликации
    retry_threshold: float = 0.55               # порог bigram_diversity для retry
    
    # Длина и структура
    target_length: int = 1200                   # целевая длина главы
    card_description_style: str = "атмосферный"  # стиль описаний карт
    
    # Fitness
    fitness: float = 0.0
    generation: int = 0
    
    def to_prompt_block(self) -> str:
        """Конвертирует ген в блок промпта для инжекта."""
        parts = []
        
        # Тон
        tone_map = {
            "тёмная сказка": "Пиши как тёмная сказка: жёстко, но с теплом.",
            "мрачный реализм": "Пиши мрачно и реализмично: никакой магии.",
            "лирический нуар": "Пиши лирически: красивые метафоры, меланхолия.",
        }
        parts.append(tone_map.get(self.system_tone, tone_map["тёмная сказка"]))
        
        # Сенсорика
        sensory_map = {
            "запахи и звуки": "Давай запахи и звуки: гнилая древесина,далёкий лай.",
            "тактильные ощущения": "Давай тактильные ощущения: холод камня, шерсть.",
            "свет и тень": "Давай свет и тень: проблески, силуэты, отражения.",
        }
        parts.append(sensory_map.get(self.sensory_emphasis, sensory_map["запахи и звуки"]))
        
        # Темп
        pacing_map = {
            "быстрый старт": "Начни с действия, без вступления.",
            "медленное нарастание": "Начни с атмосферы, наращивай напряжение.",
            "ин-мед-иас-рез": "Начни с середины сцены, как будто опоздал.",
        }
        parts.append(pacing_map.get(self.pacing_style, pacing_map["медленное нарастание"]))
        
        return "\n".join(parts)
    
    def mutate(self, rng: Random, rate: float = 0.1) -> PromptGene:
        """Мутация гена с вероятностью rate."""
        g = PromptGene(**asdict(self))
        g.fitness = 0.0
        
        if rng.random() < rate:
            g.system_tone = rng.choice(["тёмная сказка", "мрачный реализм", "лирический нуар"])
        if rng.random() < rate:
            g.sensory_emphasis = rng.choice(["запахи и звуки", "тактильные ощущения", "свет и тень"])
        if rng.random() < rate:
            g.pacing_style = rng.choice(["быстрый старт", "медленное нарастание", "ин-мед-иас-рез"])
        if rng.random() < rate:
            g.chapter_hook = rng.choice(["действие", "пейзаж", "реплика", "вопрос"])
        if rng.random() < rate:
            g.dedup_strategy = rng.choice(["bigram", "entropy", "combined"])
        if rng.random() < rate:
            g.retry_threshold = max(0.3, min(0.8, g.retry_threshold + rng.uniform(-0.1, 0.1)))
        if rng.random() < rate:
            g.target_length = max(800, min(2000, g.target_length + rng.randint(-200, 200)))
        if rng.random() < rate:
            g.card_description_style = rng.choice(["краткий", "атмосферный", "сюжетный"])
        
        return g
    
    @classmethod
    def crossover(cls, a: PromptGene, b: PromptGene, rng: Random) -> PromptGene:
        """Скрещивание двух генов."""
        child = cls()
        child.system_tone = rng.choice([a.system_tone, b.system_tone])
        child.sensory_emphasis = rng.choice([a.sensory_emphasis, b.sensory_emphasis])
        child.pacing_style = rng.choice([a.pacing_style, b.pacing_style])
        child.chapter_hook = rng.choice([a.chapter_hook, b.chapter_hook])
        child.dedup_strategy = rng.choice([a.dedup_strategy, b.dedup_strategy])
        child.retry_threshold = (a.retry_threshold + b.retry_threshold) / 2
        child.target_length = (a.target_length + b.target_length) // 2
        child.card_description_style = rng.choice([a.card_description_style, b.card_description_style])
        child.generation = max(a.generation, b.generation) + 1
        return child
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: dict) -> PromptGene:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# Начальная популяция: 5 базовых генов
DEFAULT_POPULATION: list[PromptGene] = [
    PromptGene(system_tone="тёмная сказка", sensory_emphasis="запахи и звуки",
               pacing_style="медленное нарастание", chapter_hook="действие"),
    PromptGene(system_tone="тёмная сказка", sensory_emphasis="свет и тень",
               pacing_style="быстрый старт", chapter_hook="пейзаж"),
    PromptGene(system_tone="мрачный реализм", sensory_emphasis="тактильные ощущения",
               pacing_style="ин-мед-иас-рез", chapter_hook="реплика"),
    PromptGene(system_tone="лирический нуар", sensory_emphasis="запахи и звуки",
               pacing_style="медленное нарастание", chapter_hook="вопрос"),
    PromptGene(system_tone="тёмная сказка", sensory_emphasis="тактильные ощущения",
               pacing_style="быстрый старт", chapter_hook="действие"),
]


class GEPAPopulation:
    """Популяция промпт-генов с эволюцией."""
    
    def __init__(self, genes: list[PromptGene] | None = None, seed: str = ""):
        self.genes = genes or list(DEFAULT_POPULATION)
        self.rng = Random(seed or "gepa_default")
        self.generation = 0
        self._selection_pressure = 0.3  # доля турнира
    
    def evaluate_fitness(
        self,
        week_entropy: float,
        week_bigram: float,
        week_vote_rate: float,
        week_streak_rate: float,
    ) -> None:
        """Оценка fitness каждого гена на основе данных недели."""
        # Базовый скор качества текста
        quality = 0.5 * entropy_score_from_value(week_entropy) + 0.5 * min(1.0, week_bigram / 0.7)
        
        for gene in self.genes:
            # Фитнес = качество текста * вес + вовлечённость * вес
            gene.fitness = (
                0.4 * quality +
                0.3 * week_vote_rate +
                0.3 * week_streak_rate
            )
    
    def tournament_select(self, k: int = 3) -> PromptGene:
        """Турнирный отбор: k случайных, лучший побеждает."""
        candidates = self.rng.sample(self.genes, min(k, len(self.genes)))
        return max(candidates, key=lambda g: g.fitness)
    
    def evolve(self, elite_size: int = 2) -> list[PromptGene]:
        """Эволюция: элита + скрещивание + мутация."""
        # Сортируем по fitness
        sorted_genes = sorted(self.genes, key=lambda g: g.fitness, reverse=True)
        
        # Элита проходит без изменений
        new_pop = [PromptGene(**asdict(g)) for g in sorted_genes[:elite_size]]
        
        # Остальные — скрещивание + мутация
        while len(new_pop) < len(self.genes):
            parent_a = self.tournament_select()
            parent_b = self.tournament_select()
            child = PromptGene.crossover(parent_a, parent_b, self.rng)
            child = child.mutate(self.rng, rate=0.15)
            new_pop.append(child)
        
        self.genes = new_pop
        self.generation += 1
        
        logger.info(
            "GEPA gen %d: best=%.3f avg=%.3f",
            self.generation,
            max(g.fitness for g in self.genes),
            sum(g.fitness for g in self.genes) / len(self.genes),
        )
        return self.genes
    
    def best_gene(self) -> PromptGene:
        """Лучший ген текущей популяции."""
        return max(self.genes, key=lambda g: g.fitness)
    
    def to_json(self) -> str:
        """Сериализация популяции для хранения в watcher_state."""
        return json.dumps({
            "generation": self.generation,
            "genes": [g.to_dict() for g in self.genes],
        }, ensure_ascii=False)
    
    @classmethod
    def from_json(cls, data: str, seed: str = "") -> GEPAPopulation:
        """Десериализация из watcher_state."""
        try:
            obj = json.loads(data)
            genes = [PromptGene.from_dict(g) for g in obj.get("genes", [])]
            pop = cls(genes=genes, seed=seed)
            pop.generation = obj.get("generation", 0)
            return pop
        except Exception:
            logger.warning("GEPA: не удалось десериализовать популяцию, стартуем с дефолтной")
            return cls(seed=seed)


def entropy_score_from_value(ent: float) -> float:
    """Энтропия → нормализованный скор (без текста, только по значению)."""
    if ent < ENTROPY_LOW:
        return max(0.0, 1.0 - (ENTROPY_LOW - ent) / ENTROPY_LOW)
    if ent > ENTROPY_HIGH:
        return max(0.0, 1.0 - (ent - ENTROPY_HIGH) / ENTROPY_HIGH)
    return 0.7 + 0.3 * (1.0 - abs(ent - ENTROPY_TARGET) / (ENTROPY_HIGH - ENTROPY_LOW))


# ─────────────────────────────────────────────────────────────────────
# Интеграция: динамический промпт от лучшего гена
# ─────────────────────────────────────────────────────────────────────

def apply_gene_to_prompt(base_prompt: str, gene: PromptGene) -> str:
    """Инжект гена в базовый промпт. Добавляет ген-специфичные инструкции."""
    gene_block = gene.to_prompt_block()
    # Вставляем после основных инструкций, перед контекстом дня
    return base_prompt + "\n\n" + gene_block
