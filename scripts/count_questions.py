"""Подсчёт типов вопросов в таблице тестов."""
import os, sys, asyncio, re
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
from services.sheets import sheets_cache


def _is_real_option(text: str) -> bool:
    cleaned = re.sub(r"[\d\)\(\s,;.\-]+", "", text.strip())
    return len(cleaned) > 0


async def main():
    await sheets_cache.load_all()
    total = len(sheets_cache.tests_rows)
    corr = 0
    seq = 0
    other_b = 0
    part_a = 0

    corr_examples = []
    seq_examples = []

    for row in sheets_cache.tests_rows:
        q = (row.get("Вопрос") or "").strip()
        ql = q.lower()
        has_opts = any(
            _is_real_option((row.get(f"Вар.{i}") or "").strip())
            for i in range(1, 6)
        )
        if has_opts:
            part_a += 1
        elif "соответств" in ql or "соотнес" in ql:
            corr += 1
            if len(corr_examples) < 3:
                corr_examples.append(q[:300])
        elif "последовательн" in ql or "хронологическ" in ql:
            seq += 1
            if len(seq_examples) < 3:
                seq_examples.append(q[:300])
        else:
            other_b += 1

    print(f"Всего вопросов: {total}")
    print(f"Часть A (с вариантами): {part_a}")
    print(f"Соответствие: {corr}")
    print(f"Последовательность: {seq}")
    print(f"Другие B (пропуск и т.д.): {other_b}")
    print(f"Итого B для форматирования: {corr + seq}")
    print()
    print("=== Примеры вопросов на соответствие ===")
    for i, ex in enumerate(corr_examples, 1):
        print(f"\n--- {i} ---\n{ex}")
    print()
    print("=== Примеры вопросов на последовательность ===")
    for i, ex in enumerate(seq_examples, 1):
        print(f"\n--- {i} ---\n{ex}")


asyncio.run(main())
