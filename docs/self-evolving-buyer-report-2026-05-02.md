# Self-Evolving Buyer: отчет и план замыкания learning loop

## Статус

- Дата: 2026-05-02.
- Назначение: исследовательский и архитектурный отчет о том, как превратить текущий `buyer` из агента с post-run рекомендациями в self-improving/self-evolving систему с контролируемым циклом изменений.
- Граница: документ не меняет runtime-поведение. Все предложения ниже требуют отдельной реализации, eval-gate и review.
- Важное ограничение: LLM рассматривается как неизменяемая foundation model. В этом плане нет дообучения, LoRA, RLHF/RLAIF, persistent weight updates или обучения отдельной memory model. Менять можно только обвязку вокруг LLM: prompts, demonstrations, external memory, site profiles, playbooks, scripts, tool policy, eval cases, model routing и код orchestration/eval.
- Ключевое решение: self-evolution для `buyer` должен быть не автономным self-modifying runtime, а управляемым контуром `capture -> evaluate -> diagnose -> propose -> validate -> approve -> activate -> monitor`.

## Executive Summary

В текущем репозитории уже есть две половины learning loop:

- `buyer` после завершения сессии запускает `PostSessionKnowledgeAnalyzer` и пишет `knowledge-analysis.json` с draft knowledge, pitfalls и playbook candidate.
- `eval_service` запускает batch eval cases, собирает trace, строит redacted `judge-input.json`, запускает LLM Judge и пишет `evaluation.json` с checks и draft recommendations.

Пробел: результаты анализа и judge-рекомендации остаются файловыми артефактами. Они не превращаются в единый lifecycle кандидатов, не проходят review/activation, не валидируются на regression suite и не подмешиваются в следующий runtime-прогон.

Минимально правильный следующий шаг: добавить центральный `candidate store` и `activation layer`, который индексирует `knowledge-analysis.json` и `evaluation.json`, хранит кандидатов в статусах `draft/reviewed/active/rejected/archived`, связывает их с evidence refs и позволяет `AgentRunner` читать только approved/active domain knowledge при сборке prompt.

## Текущее состояние в коде

### Buyer

- `buyer/app/service.py`: главный orchestrator сессии. Здесь запускаются auth-flow, purchase scripts-first, generic `codex exec`, callbacks, финализация и post-session analysis.
- `buyer/app/runner.py`: готовит trace-контекст, prompt, запускает `codex exec`, пишет `step-XXX-prompt.txt`, `step-XXX-trace.json`, `step-XXX-browser-actions.jsonl`.
- `buyer/app/prompt_builder.py`: основной prompt агента `buyer`. Сейчас в prompt попадают task, metadata, auth summary, user profile, memory и latest user reply, но не попадают approved site profile/playbook из post-run learning.
- `buyer/app/knowledge_analyzer.py`: post-session analyzer. Уже строит redacted analysis input и пишет draft knowledge в trace-dir, но результат не индексируется как reusable knowledge.
- `buyer/app/purchase_scripts.py` и `buyer/app/auth_scripts.py`: registry скриптов. Сейчас lifecycle скриптов описан концептуально, но registry фактически hardcoded и не связан с review/activation.

### Eval Service

- `eval_service/app/orchestrator.py`: последовательный запуск eval cases через обычный API `buyer`.
- `eval_service/app/api.py`: endpoints для runs, judge и dashboard. После judge результат сохраняется в `evaluation.json`, но recommendations не превращаются в отдельные review candidates.
- `eval_service/app/judge_runner.py`: запускает judge через `codex exec --output-schema`.
- `eval_service/app/judge_prompt.py`: prompt LLM Judge. Уже требует evidence refs и draft recommendations.
- `eval_service/app/trace_collector.py`: собирает trace summary по эвристическим паттернам файлов.
- `eval_service/app/aggregation.py`: агрегирует evaluations и baseline по duration/tokens, но пока не считает regression/drift signals по версиям prompts/scripts/site profiles.

### Документация и существующие решения

- `docs/buyer.md`: фиксирует post-session knowledge analysis и запрет автоприменения draft knowledge.
- `docs/architecture-decisions.md`: фиксирует eval loop как learning loop, а не release gate, и запрещает автоприменение judge recommendations.
- `docs/buyer-roadmap.md`: уже содержит будущие задачи review/activation flow, lifecycle script/playbook candidates, site profiles, strategy ranking и artifact manifest.

## Главный архитектурный разрыв

Сейчас система умеет:

1. Исполнять сессию покупки.
2. Собирать богатые browser/action/trace артефакты.
3. Оценивать прогон LLM Judge.
4. Генерировать draft knowledge/recommendations.

Но система не умеет:

1. Нормализовать все предложения улучшений в единую модель кандидата.
2. Привязывать кандидата к версии prompt/script/tool-policy, домену, eval-case и evidence refs.
3. Сравнивать кандидата с baseline на replay/eval suite.
4. Утверждать/отклонять кандидата через UI/API.
5. Активировать только безопасные approved знания в runtime prompt.
6. Откатывать active candidate при регрессии.

Пока этого слоя нет, `buyer` не self-improving, а только self-reporting.

## Свежие подходы SotA на 2026-05-02

### Наиболее применимые сразу

Критерий отбора: подход должен работать без изменения весов foundation model. Практически применимы только методы, которые эволюционируют prompt, внешнюю память, skills/scripts, workflow config, evaluator rubric или runtime-обвязку.

| Подход | Источник | Что важно для `buyer` |
| --- | --- | --- |
| GEPA: reflective prompt evolution | [arXiv 2507.19457](https://arxiv.org/abs/2507.19457), v2 от 2026-02-14, ICLR 2026 Oral | Использовать trajectory traces и natural-language reflection для мутаций prompt, затем выбирать Pareto-кандидаты по success, safety, cost и duration. |
| SkillWeaver | [arXiv 2504.07079](https://arxiv.org/abs/2504.07079) | Превратить успешные/исправленные web trajectories в typed Playwright skills/API с preconditions, postconditions и verifier. |
| ReasoningBank | [arXiv 2509.25140](https://arxiv.org/abs/2509.25140), v2 от 2026-03-16, ICLR 2026 | Дистиллировать успешные и провальные buyer trajectories в reusable reasoning memories, а не хранить только raw trace или только успешные routines. |
| AgentRewardBench | [arXiv 2504.08942](https://arxiv.org/abs/2504.08942), v2 от 2025-10-06 | Калибровать LLM Judge для web trajectories: оценивать success, side effects, repetition, а не полагаться только на rule-based check. |
| WebGraphEval | [arXiv 2510.19205](https://arxiv.org/abs/2510.19205) | Представлять browser trajectories как граф действий, чтобы находить критические развилки, избыточные петли и неэффективные переходы. |
| AlphaEvolve | [DeepMind blog, 2025-05-14](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/) | Для merchant adapters и Playwright scripts: LLM предлагает code mutations, evaluators отбирают только проверенные улучшения. |
| Darwin Godel Machine | [arXiv 2505.22954](https://arxiv.org/abs/2505.22954), v3 от 2026-03-12 | Вести архив вариантов prompts/scripts/tools и принимать изменения только после sandboxed benchmarks и human oversight. |
| SE-Agent | [arXiv 2508.02085](https://arxiv.org/abs/2508.02085), v6 от 2025-11-03 | Использовать revision/recombination/refinement нескольких trajectories для предложения recovery plan или script candidate. |
| EvoFlow | [arXiv 2502.07373](https://arxiv.org/abs/2502.07373) | Искать не один лучший workflow, а population: script-first, generic-first, recovery-heavy, handoff-early, cheaper-model flow. |
| DSPy optimizers | [DSPy docs](https://dspy.ai/learn/optimization/optimizers/) | Для узких LM-компонентов можно оптимизировать instructions/demos по метрике eval suite, начиная с малых train/dev sets. |

### Применимо как research track

| Подход | Источник | Почему не первый шаг |
| --- | --- | --- |
| WebEvolver | [arXiv 2504.21024](https://arxiv.org/abs/2504.21024), EMNLP 2025 | Co-evolving world model для web-agent перспективен, но требует отдельного world-model/data pipeline. На старте лучше использовать recorded fixtures. |
| WebGym | [arXiv 2601.02439](https://arxiv.org/abs/2601.02439), v5 от 2026-02-26 | Масштабный RL на сотнях тысяч web tasks. Для текущего `buyer` сначала нужны replayable eval cases и candidate lifecycle. |
| Agent Lightning | [arXiv 2508.03680](https://arxiv.org/abs/2508.03680) | Полезная training-agent disaggregation архитектура, но требует RL/fine-tuning pipeline. При текущем ограничении можно заимствовать только separation of execution/training/evaluation, без weight updates. |
| SEAL | [arXiv 2506.10943](https://arxiv.org/abs/2506.10943) | Persistent weight updates не подходят как первый production loop из-за safety, reproducibility и approval рисков. |
| Self-Challenging Agents | [arXiv 2506.01716](https://arxiv.org/abs/2506.01716) | Генерация задач и verifier functions полезна, но RL-часть стоит отложить до появления стабильного verifier corpus. |
| MEM1/MemGen | [MEM1 arXiv 2506.15841](https://arxiv.org/abs/2506.15841), [MemGen arXiv 2509.24704](https://arxiv.org/abs/2509.24704) | Идеи memory consolidation важны, но текущему `buyer` сначала нужен явный external memory lifecycle, а не обучение memory model. |

## Целевая архитектура self-evolution loop

```text
buyer session
  -> immutable run bundle
  -> eval_service judge + deterministic checks
  -> diagnosis
  -> patch/knowledge/script candidate
  -> validation on replay + eval suite
  -> human approval
  -> activation in versioned registry
  -> runtime consumption by AgentRunner/prompt/script registry
  -> monitoring and drift alerts
```

### 1. Capture

Каждый прогон должен иметь immutable run bundle:

- task, start URL, host, metadata, auth summary без секретов;
- версии prompt, model, CDP tool, scripts, site profile, active memories;
- callbacks и финальный session state;
- browser actions JSONL;
- DOM/a11y snapshots, screenshots, HTML fallback refs, script traces;
- handoff action log, если был handoff;
- payment boundary evidence;
- trace manifest, чтобы `eval_service` не искал файлы эвристически.

### 2. Evaluate

Eval должен быть смесью deterministic checks и LLM Judge:

- deterministic: payment boundary, SberPay-only policy, отсутствие final payment click, verifier для supported domains, schema validation, token/time/step budgets;
- LLM Judge: task outcome, товар/вариант/адрес, side effects, повторяющиеся петли, качество рекомендаций, evidence sufficiency;
- process rewards: штрафы за повторные full HTML dumps, навигационные петли, неверный payment method, premature ask_user, отсутствие проверки selected variant.

### 3. Diagnose

Нужна явная taxonomy root causes:

- `perception_dom_grounding`: не распознан элемент, неверный selector, visual-only UI;
- `navigation_loop`: повторная навигация, возврат в уже пройденное состояние;
- `product_mismatch`: товар, размер, цвет, формат или количество не соответствуют task;
- `auth_blocker`: auth не подготовлен или login required;
- `delivery_blocker`: адрес/доставка/самовывоз требуют решения;
- `payment_boundary`: SberPay не найден, перепутан с СБП/SBP/FPS, нет evidence;
- `tool_failure`: CDP/Playwright/script failure;
- `site_drift`: DOM/flow изменился относительно active script/profile;
- `prompt_gap`: prompt не содержит нужного правила или слишком общий;
- `eval_gap`: case/rubric не покрывает важную ситуацию.

### 4. Propose

Все предложения должны приводиться к типизированному `EvolutionCandidate`:

```json
{
  "candidate_id": "cand-...",
  "source": "eval_judge|knowledge_analysis|manual|drift_detector",
  "target_surface": "prompt|reasoning_memory|site_profile|playbook|script|eval_case|tool_policy",
  "scope": {"host": "litres.ru", "eval_case_id": "litres_book_odyssey_001"},
  "priority": "low|medium|high",
  "risk": "low|medium|high|critical",
  "status": "draft|reviewed|active|rejected|archived",
  "rationale": "...",
  "evidence_refs": [],
  "draft_diff": "...",
  "expected_metric_effect": {"success_rate": "+", "duration_ms": "-", "safety": "no_regression"},
  "required_validation": ["unit", "eval_suite", "payment_boundary", "human_review"]
}
```

`EvolutionCandidate` не должен описывать изменение весов LLM. Если judge или analyzer предлагает "дообучить модель", такое предложение нормализуется в одну из допустимых поверхностей: prompt patch, few-shot demonstration, external memory, evaluator rubric, script/playbook candidate или workflow/tool-policy patch.

### 5. Validate

Candidate нельзя активировать без validation report:

- unit tests на измененный модуль;
- eval cases на затронутом host;
- cross-host regression suite;
- replay/sandbox для script candidates;
- static safety checks на prompt/script;
- comparison с baseline: success, `not_ok`, duration, tokens, handoff rate, retry count, payment boundary violations.

### 6. Approve

Human approval обязателен для:

- любого изменения payment boundary, SberPay verifier, checkout policy;
- любого script candidate, который может кликать checkout/payment UI;
- активных site profiles/playbooks для реальных магазинов;
- изменения auth/handoff/CAPTCHA поведения;
- изменения prompts, которое ослабляет safety invariant.

### 7. Activate

Runtime должен читать только active candidates:

- `AgentRunner` получает active site profile/reasoning memories/playbook по exact host;
- `prompt_builder` добавляет отдельные data-блоки, не как инструкции более высокого приоритета;
- script registry выбирает только active/published scripts;
- все active artifacts имеют version, provenance и rollback path.

## Конкретные изменения по файлам

### Candidate Store

- Создать `eval_service/app/evolution_candidates.py`.
- Создать `eval_service/tests/test_evolution_candidates.py`.
- Добавить filesystem layout:
  - `eval/runs/<eval_run_id>/candidates/<candidate_id>.json`;
  - `eval/candidates/index.json` или отдельный catalog по мере роста.
- В `eval_service/app/api.py` после `_persist_judge_result` извлекать `evaluation.recommendations` и сохранять их как candidates.
- Добавить endpoints:
  - `GET /candidates`;
  - `GET /candidates/{candidate_id}`;
  - `POST /candidates/{candidate_id}/review`;
  - `POST /candidates/{candidate_id}/activate`;
  - `POST /candidates/{candidate_id}/reject`.

### Knowledge Analysis

- В `buyer/app/knowledge_analysis_schema.json` добавить поля:
  - `target_surface`;
  - `activation_risk`;
  - `applicability`;
  - `required_validation`.
- В `buyer/app/knowledge_analyzer.py` расширить prompt: analyzer должен отличать site profile, playbook, prompt patch, script candidate и eval gap.
- Добавить экспорт knowledge-analysis candidates в тот же `EvolutionCandidate` формат или отдельный ingest endpoint в `eval_service`.

### Runtime Consumption

- Создать `buyer/app/site_knowledge.py` для чтения approved/active domain knowledge.
- В `buyer/app/settings.py` добавить путь к active knowledge registry, например `BUYER_ACTIVE_KNOWLEDGE_DIR`.
- В `buyer/app/runner.py` перед `build_agent_prompt()` загрузить active exact-domain knowledge.
- В `buyer/app/prompt_builder.py` добавить блоки:
  - `<active_site_profile_json>`;
  - `<active_playbook_json>`;
  - `<active_reasoning_memories_json>`.
- В prompt явно указать, что эти блоки являются reviewed data, но не могут отменять hard invariants.

### Script/Skill Evolution

- Создать manifest `buyer/scripts/registry.yaml`.
- В `buyer/app/purchase_scripts.py` и `buyer/app/auth_scripts.py` читать registry с lifecycle `draft/review/published/disabled`.
- Добавить `script_candidate` schema:
  - domain, preconditions, actions, expected evidence, verifier, forbidden actions, test cases.
- Добавить sandbox/dry-run command для script candidates, который запускает candidate только на eval/replay окружении.

### Trace Manifest

- В `buyer/app/runner.py` и script runners писать `manifest.json` в session trace-dir.
- В manifest хранить prompt path, trace path, browser actions path, screenshots, script traces, model attempts, prompt hash, active candidate versions.
- В `eval_service/app/trace_collector.py` сначала читать manifest, а эвристический поиск оставить fallback.

### Regression/Drift Analytics

- В `eval_service/app/aggregation.py` добавить группировку по версиям:
  - prompt hash/version;
  - active site profile version;
  - script version;
  - model strategy;
  - host.
- Добавить signals:
  - `success_rate_delta`;
  - `payment_boundary_not_ok_delta`;
  - `duration_ms_delta`;
  - `buyer_tokens_delta`;
  - `handoff_rate_delta`;
  - `loop_or_repetition_rate`.

### UI

- В `micro-ui/app/static/eval.js` добавить раздел candidates рядом с evaluations:
  - список draft/reviewed/active/rejected;
  - evidence refs;
  - rendered diff/draft text;
  - validation status;
  - approve/reject/activate controls.
- В `micro-ui/app/static/eval.css` добавить компактные states для candidate risk/status.

## Рекомендованная очередность внедрения

### Phase 0: Guardrails и contracts

1. Зафиксировать `EvolutionCandidate` JSON schema.
2. Зафиксировать status lifecycle.
3. Зафиксировать approval policy.
4. Добавить tests на path safety, redaction и evidence refs.

### Phase 1: Candidate ingestion

1. Сохранять eval recommendations как candidates.
2. Индексировать `knowledge-analysis.json` как candidates.
3. Добавить API списка и просмотра candidates.
4. Добавить UI read-only.

### Phase 2: Review/activation

1. Добавить approve/reject/activate API.
2. Добавить active registry.
3. Добавить audit trail: кто, когда, почему активировал.
4. Добавить rollback состояния active candidate.

### Phase 3: Runtime use

1. Подключить active site profile/reasoning memories к prompt.
2. Ограничить scope exact host и exact surface.
3. Добавить тесты, что active knowledge не может отменить hard invariants.
4. Добавить eval cases для проверки, что approved knowledge реально влияет на trajectory.

### Phase 4: Prompt evolution

1. Сделать offline GEPA-like runner: input evaluations + traces, output prompt candidate diff.
2. Использовать Pareto selection: success/safety/duration/tokens.
3. Прогонять candidate prompt на eval suite.
4. Активировать только через human approval.

### Phase 5: Script/skill evolution

1. Ввести script registry manifest.
2. Генерировать SkillWeaver-style skills с pre/postconditions.
3. Запускать AlphaEvolve-style mutations только в sandbox.
4. Публиковать scripts через lifecycle `draft -> review -> published`.

### Phase 6: Workflow evolution и research

1. Ввести workflow config population: `script_first`, `generic_first`, `handoff_early`, `recovery_heavy`, `cheap_model_first`.
2. Применить EvoFlow-style selection по host/case tags.
3. Исследовать WebEvolver/WebGym/Agent Lightning только после появления стабильного replay/eval corpus.

## Что должен предлагать orchestrator после оценки

После каждого judge batch orchestrator должен создавать не только текстовые рекомендации, а конкретные кандидаты:

- prompt patch: изменение в `buyer/app/prompt_builder.py` или prompt template registry;
- reasoning memory: краткое правило для exact host, например “на Litres SberPay evidence появляется в iframe payecom после выбора Российской карты”;
- site profile: selectors, checkout landmarks, anti-patterns, known blockers;
- playbook: параметризованный путь `search -> product -> cart -> checkout -> payment evidence`;
- script candidate: TypeScript Playwright module draft с verifier и forbidden actions;
- eval case: новый regression case из провала или drift signal;
- tool policy patch: изменение CDP tool guidance, timeout, retry или observation strategy.

Каждый кандидат должен иметь:

- evidence refs;
- expected effect;
- risk;
- validation checklist;
- owner/reviewer;
- activation scope.

## Safety policy для self-evolution

Нельзя автоматически активировать:

- ослабление запрета реального платежа;
- замену SberPay на СБП/SBP/FPS;
- обход CAPTCHA без handoff;
- сохранение auth/cookies/storageState/tokens;
- скрипты, которые кликают final payment/confirm buttons;
- wildcard site profiles без exact domain scope;
- prompt patches, которые меняют приоритет hard invariants.

Можно автоматически создавать как draft:

- negative knowledge;
- eval case candidates;
- site drift notes;
- prompt improvement suggestions;
- readonly selector hypotheses;
- script skeletons без publication.

## Метрики готовности

`buyer` можно считать self-improving на первом уровне, когда:

- 100% judge recommendations сохраняются как candidates или явно отбрасываются с причиной.
- Есть UI/API review для candidates.
- Active domain knowledge используется в следующем prompt и имеет version/provenance.
- Любой active candidate имеет validation report.
- Regression dashboard показывает влияние active candidates.

`buyer` можно считать self-evolving на втором уровне, когда:

- Prompt candidates генерируются из eval traces автоматически.
- Script/skill candidates генерируются из successful/handoff trajectories.
- Workflow configs выбираются по host/case tags и Pareto metrics.
- Есть rollback и staged rollout.
- Safety violations остаются нулевыми на regression suite.

## Итоговая рекомендация

Начинать нужно не с обучения весов и не с автономного self-modifying agent. Самый короткий и безопасный путь для текущего кода:

1. Единый `EvolutionCandidate` store.
2. Review/activation API и UI.
3. Runtime consumption только approved exact-domain knowledge.
4. GEPA-like prompt candidate generator поверх `eval_service`.
5. SkillWeaver/AlphaEvolve-style script candidate generator в sandbox.
6. Regression/drift dashboard по версиям active candidates.

Такой порядок использует уже реализованные trace, judge и knowledge-analysis механизмы, но добавляет отсутствующее звено: контролируемую эволюцию самого агента через версионируемые изменения кода, промптов, playbook и скриптов.
