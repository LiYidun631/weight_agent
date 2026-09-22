import asyncio
import json
import statistics
import time
from pathlib import Path

from weight_agent.api.schemas.report import ReportAnalysis, ReportRequest
from weight_agent.core.config import Settings
from weight_agent.report.qwen import QwenReportAnalyzer

MODELS = ["qwen3.8-flash", "qwen3.7-flash", "qwen-turbo"]
RUNS_PER_MODEL = 3


def sample_request() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "measurement_id": "synthetic-benchmark-only",
            "measured_at": "2026-09-21T10:00:00+08:00",
            "subject": {
                "sex": "male",
                "height_cm": {"value": 172, "unit": "cm"},
                "age_years": {"value": 28, "unit": "岁"},
            },
            "body_composition": {
                "weight_kg": {
                    "value": 62.3,
                    "unit": "kg",
                    "level": "normal",
                    "standard_min": 55.3,
                    "standard_max": 74.9,
                },
                "total_body_water_kg": {
                    "value": 35.2,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 36.5,
                    "standard_max": 44.7,
                },
                "body_fat_mass_kg": {
                    "value": 14.3,
                    "unit": "kg",
                    "level": "normal",
                    "standard_min": 7.8,
                    "standard_max": 15.6,
                },
                "protein_mass_kg": {
                    "value": 8.9,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 9.8,
                    "standard_max": 11.9,
                },
                "mineral_mass_kg": {
                    "value": 3.3,
                    "unit": "kg",
                    "level": "normal",
                    "standard_min": 3.3,
                    "standard_max": 4.1,
                },
                "fat_free_mass_kg": {
                    "value": 48.0,
                    "unit": "kg",
                    "level": "normal",
                    "standard_min": 47.5,
                    "standard_max": 59.3,
                },
                "muscle_mass_kg": {
                    "value": 44.7,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 47.0,
                    "standard_max": 64.6,
                },
                "bone_mass_kg": {
                    "value": 2.6,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 2.8,
                    "standard_max": 3.5,
                },
                "skeletal_muscle_mass_kg": {
                    "value": 26.4,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 27.8,
                    "standard_max": 34.0,
                },
                "intracellular_water_kg": {
                    "value": 22.0,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 22.7,
                    "standard_max": 27.7,
                },
                "extracellular_water_kg": {
                    "value": 13.1,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 13.9,
                    "standard_max": 17.0,
                },
                "body_cell_mass_kg": {
                    "value": 31.6,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 32.5,
                    "standard_max": 39.7,
                },
                "subcutaneous_fat_mass_kg": {"value": 12.9, "unit": "kg"},
            },
            "segmental_composition": {
                "fat_mass_kg": {
                    "right_arm": {"value": 0.8, "unit": "kg"},
                    "left_arm": {"value": 0.9, "unit": "kg"},
                    "trunk": {"value": 7.2, "unit": "kg"},
                    "right_leg": {"value": 1.8, "unit": "kg"},
                    "left_leg": {"value": 1.9, "unit": "kg"},
                },
                "fat_rate_percent": {
                    "right_arm": {"value": 12.4, "unit": "%", "level": "normal"},
                    "left_arm": {"value": 12.8, "unit": "%", "level": "normal"},
                    "trunk": {"value": 25.6, "unit": "%", "level": "high"},
                    "right_leg": {"value": 20.1, "unit": "%", "level": "normal"},
                    "left_leg": {"value": 20.6, "unit": "%", "level": "normal"},
                },
                "muscle_mass_kg": {
                    "right_arm": {"value": 2.3, "unit": "kg"},
                    "left_arm": {"value": 2.5, "unit": "kg"},
                    "trunk": {"value": 21.2, "unit": "kg"},
                    "right_leg": {"value": 7.5, "unit": "kg"},
                    "left_leg": {"value": 7.7, "unit": "kg"},
                },
                "muscle_rate_percent": {
                    "right_arm": {"value": 35.2, "unit": "%", "level": "low"},
                    "left_arm": {"value": 35.6, "unit": "%", "level": "low"},
                    "trunk": {"value": 47.8, "unit": "%", "level": "normal"},
                    "right_leg": {"value": 37.9, "unit": "%", "level": "low"},
                    "left_leg": {"value": 38.3, "unit": "%", "level": "low"},
                },
            },
            "assessment": {
                "body_score": {"value": 66, "unit": "分"},
                "body_age": {"value": 19, "unit": "岁"},
                "body_type": {"code": 4, "name": "浮肿肥胖型"},
                "skeletal_muscle_index": {"value": 7.8},
                "waist_hip_ratio": {
                    "value": 0.79,
                    "level": "low",
                    "standard_min": 0.80,
                    "standard_max": 0.90,
                },
                "visceral_fat_level": {
                    "value": 5,
                    "unit": "级",
                    "level": "normal",
                    "standard_min": 1,
                    "standard_max": 9,
                },
                "obesity_degree_percent": {
                    "value": 95,
                    "unit": "%",
                    "level": "normal",
                    "standard_min": 90,
                    "standard_max": 110,
                },
                "bmi": {
                    "value": 21.1,
                    "unit": "kg/m2",
                    "level": "normal",
                    "standard_min": 18.5,
                    "standard_max": 23.0,
                },
                "body_fat_rate_percent": {
                    "value": 22.9,
                    "unit": "%",
                    "level": "high",
                    "standard_min": 10.0,
                    "standard_max": 20.0,
                },
                "basal_metabolism_kcal": {
                    "value": 1406,
                    "unit": "kCal",
                    "level": "normal",
                    "standard_min": 1399,
                    "standard_max": 1628,
                },
                "recommended_calorie_intake_kcal": {"value": 1827, "unit": "kCal"},
                "ideal_body_weight_kg": {"value": 65.1, "unit": "kg"},
                "target_weight_kg": {"value": 65.1, "unit": "kg"},
                "weight_control_kg": {"value": 2.8, "unit": "kg"},
                "muscle_control_kg": {"value": 7.3, "unit": "kg"},
                "fat_control_kg": {"value": -4.5, "unit": "kg"},
                "subcutaneous_fat_rate_percent": {
                    "value": 20.7,
                    "unit": "%",
                    "level": "high",
                    "standard_min": 8.6,
                    "standard_max": 16.7,
                },
            },
            "exercise_calories_kcal_per_30_min": {
                "walking": {"value": 124, "unit": "kCal/30min"},
                "golf": {"value": 109, "unit": "kCal/30min"},
                "gateball": {"value": 118, "unit": "kCal/30min"},
                "tennis_cycling_basketball": {"value": 186, "unit": "kCal/30min"},
                "squash_shuttlecock_taekwondo_fencing": {
                    "value": 311,
                    "unit": "kCal/30min",
                },
                "climbing": {"value": 203, "unit": "kCal/30min"},
                "swimming_aerobics_jogging_football_jumping_rope": {
                    "value": 217,
                    "unit": "kCal/30min",
                },
                "badminton_table_tennis": {"value": 140, "unit": "kCal/30min"},
            },
            "segment_standards": {
                "fat": {
                    "right_arm": 1,
                    "left_arm": 1,
                    "trunk": 2,
                    "right_leg": 1,
                    "left_leg": 1,
                },
                "muscle": {
                    "right_arm": 0,
                    "left_arm": 0,
                    "trunk": 1,
                    "right_leg": 0,
                    "left_leg": 0,
                },
            },
        }
    )


def score(result: ReportAnalysis) -> dict[str, object]:
    text = result.overall_assessment + " " + " ".join(x.content for x in result.interventions)
    terms = {
        "muscle": ("肌肉", "骨骼肌"),
        "fat": ("脂肪", "体脂"),
        "metabolism": ("代谢", "基础代谢"),
        "water": ("水分", "细胞内", "细胞外"),
        "protein": ("蛋白",),
        "control_amount": ("7.3", "4.5", "增肌", "减脂"),
    }
    coverage = {
        name: any(term in text for term in candidates) for name, candidates in terms.items()
    }
    return {
        "coverage": coverage,
        "coverage_count": sum(coverage.values()),
        "has_interventions": bool(result.interventions),
        "text_length": len(text),
    }


async def run_one(model: str, request: ReportRequest, settings: Settings) -> dict[str, object]:
    analyzer = QwenReportAnalyzer(
        api_key=settings.dashscope_api_key.get_secret_value(),
        base_url=settings.report_model_base_url,
        model=model,
        request_timeout_seconds=15,
    )
    started = time.perf_counter()
    try:
        result = await analyzer.analyze(request, user_id="synthetic-benchmark")
    except Exception as exc:
        return {
            "model": model,
            "ok": False,
            "latency_seconds": round(time.perf_counter() - started, 3),
            "error": type(exc).__name__,
        }
    return {
        "model": model,
        "ok": True,
        "latency_seconds": round(time.perf_counter() - started, 3),
        **score(result),
        "response": result.model_dump(),
    }


async def main() -> None:
    settings = Settings()
    if settings.dashscope_api_key is None:
        raise SystemExit("DASHSCOPE_API_KEY is not configured")
    request = sample_request()
    results = []
    for model in MODELS:
        for run in range(1, RUNS_PER_MODEL + 1):
            item = await run_one(model, request, settings)
            item["run"] = run
            results.append(item)
            print(json.dumps(item, ensure_ascii=False))
    summary = {}
    for model in MODELS:
        rows = [x for x in results if x["model"] == model]
        ok = [x for x in rows if x["ok"]]
        latencies = [float(x["latency_seconds"]) for x in ok]
        summary[model] = {
            "runs": len(rows),
            "successes": len(ok),
            "success_rate": round(len(ok) / len(rows), 3),
            "avg_latency_seconds": round(statistics.mean(latencies), 3) if latencies else None,
            "p95_latency_seconds": round(max(latencies), 3) if latencies else None,
            "avg_coverage_count": round(statistics.mean([int(x["coverage_count"]) for x in ok]), 2)
            if ok
            else None,
        }
    output = {
        "models": MODELS,
        "runs_per_model": RUNS_PER_MODEL,
        "summary": summary,
        "results": results,
    }
    path = Path("tmp/report-model-benchmark.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "output": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
