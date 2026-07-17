# 로컬 LLM 오케스트레이션 정책 연구

> 상태: **종료 및 보존**  
> 실험 기간: **2026-07-15~2026-07-16**  
> 연구 구현 기준 커밋: **`9d37b59`**  
> 종료 문서 커밋: **`90510db`**  
> Frontier-v4 sealed holdout 64개: **미개봉**

이 문서는 Gemma 4 E4B와 Qwen 3.5 9B를 개인용 LLM 오케스트레이션의 로컬 정책 모델로 사용할 수 있는지 검증한 연구 기록입니다. 정책 모델은 사용자에게 답을 직접 보여 주는 모델이 아니라, 작업 종류와 위험을 판단하고 어떤 클라우드 모델을 몇 번 호출할지 결정하는 내부 조정 모델을 뜻합니다.

현재 Meapri Agent Hub 저장소에는 통합 오케스트레이션 구현만 남아 있고, 대용량 학습 자료와 실험 코드는 예전 `Chanwoo-act/agent-hub` 체크아웃에 보존돼 있습니다. 이 문서의 근거 경로는 모두 해당 보존 저장소를 기준으로 적었습니다. 이 보고서는 결과를 이해하기 위한 독립 문서이며, 전체 실험을 재현하려면 마지막의 근거 문서와 고정 커밋을 함께 복구해야 합니다.

## 결론

연구 결과는 다음 네 문장으로 요약할 수 있습니다.

1. Gemma 4 E4B는 작업 분류와 엄격한 JSON 출력 계약을 학습했습니다.
2. 그러나 Claude, GPT, Gemini, Grok 가운데 더 나은 모델을 고르는 능력은 독립 검증 세트와 봉인 평가에서 안정적으로 일반화되지 않았습니다.
3. Qwen 3.5 9B의 MLX LoRA 학습은 이번 소프트웨어 조합과 Mac 환경에서 실행 가능성 기준을 통과하지 못했습니다.
4. 학습하지 않은 Qwen에 프롬프트와 결정적 상태 기계를 붙인 방식은 제한된 실행 경로 두 건에서 작동했지만, 별도 로컬 조정 모델을 유지할 만큼의 품질·속도·비용 우위는 입증하지 못했습니다.

따라서 로컬 정책 모델을 활성 런타임으로 승격하지 않았습니다. 현재 개인 사용 조건에서는 강한 클라우드 모델이 계획을 만들고, 최대 호출 횟수·단계 의존성·독립 검증·완료 조건은 로컬의 결정적 코드가 제한하는 방식이 더 단순하고 검증하기 쉽다는 결론을 내렸습니다.

이 결론은 로컬 LLM 오케스트레이션이 원리적으로 불가능하다는 뜻이 아닙니다. 이번 하드웨어, 모델, 학습 목표, 데이터 규모와 평가 방식에 한정된 중단 결정입니다. 모든 제공자의 비용을 같은 기준으로 측정하지 못했으므로 클라우드 방식의 비용 우위도 주장하지 않습니다.

## 연구 질문

연구는 세 가지 질문에서 출발했습니다.

1. 보이지 않는 로컬 모델이 작업 능력, 주 실행 모델, 최대 호출 횟수와 검증 방식을 결정할 수 있는가?
2. 생각·실행·검증 역할을 단계별로 조직하고 실패를 복구하는 여러 단계 조정 모델로 동작할 수 있는가?
3. 정적 기준선보다 실제 작업 결과가 좋은 모델을 더 자주 고르면서 호출 수와 검증 계약을 지킬 수 있는가?

첫 번째 질문의 형식 부분에는 긍정적인 결과가 있었습니다. 두 번째 질문은 두 건의 제한된 실행으로 배선 가능성만 확인했습니다. 세 번째 질문은 승격에 필요한 근거를 얻지 못했습니다.

## 성공 판정 기준

JSON이 파싱된다는 사실만으로 후보를 승격하지 않았습니다. 최종 판정에는 다음 기준을 사용했습니다.

- 미리 선언한 정적 기준선보다 주 실행 모델 선택 정확도가 높아야 합니다.
- 잘못 선택했을 때의 품질 손실인 `fail-closed regret`이 기준선보다 낮아야 합니다.
- 언어, 작업 종류와 허용 호출 횟수별 구간에서 큰 회귀가 없어야 합니다.
- Frontier-v4에서는 같은 원본 작업의 예산 변형을 한 묶음으로 다시 뽑는 scenario-cluster bootstrap의 95% 신뢰 구간도 개선을 지지해야 합니다.
- 평가용 holdout은 prompt, adapter, policy, code, 추론 설정과 정답 없는 예측을 먼저 동결한 뒤에만 열 수 있습니다.

잘못된 출력은 평균 계산에서 빼지 않고 해당 작업에서 관측된 최악의 regret으로 처리했습니다. 형식을 못 지키는 후보가 평가에서 유리해지지 않도록 하기 위한 fail-closed 원칙입니다.

## 실험 환경과 고정 모델

### 로컬 환경

- Apple Silicon Mac, 48GB unified memory
- MLX와 MLX-VLM
- 실제 클라우드 모델 호출을 실행하는 Orca terminal runtime

### 로컬 후보 모델

| 후보 | 고정 revision | 용도와 결과 |
| --- | --- | --- |
| `mlx-community/gemma-4-e4b-it-4bit` | `475b9088d29754a3379866cf5aeb6b41acd313c2` | QLoRA 정책 모델. 형식 학습에는 성공했지만 모델 선택은 일반화되지 않음 |
| `mlx-community/Qwen3.5-9B-MLX-4bit` | `938d8919941c6e7efd3c7150eff7fe9d12afa631` | LoRA 실행 가능성 실패 후 prompt-only 조정 모델로 제한 실험 |
| `mlx-community/gemma-4-12B-it-qat-4bit` | `e70c6b3ba0979b3357dcd2f223ad8bde7787a6b6` | 후보 정보만 고정. 실제 다운로드·추론·학습 비교는 하지 않음 |

Gemma와 Qwen 학습에는 MLX-VLM revision `cef92e2c5990d9c9ae53937c3e8664983275e4e2`를 사용했습니다. 정확한 모델 출처와 라이선스는 `MODEL.lock`, `MODEL-QWEN35.lock`, `MODEL-GEMMA4-12B.lock`에 남아 있습니다.

### 결과 수집에 사용한 네 모델

- GPT-5.6 Sol
- Claude Opus 4.8
- Gemini 3.1 Pro High — Antigravity `agy` 경유
- Grok 4.5 High

초기 이중언어 실험에서는 GPT 계열 3종, Claude 2종, Gemini 2종, Grok 1종의 여덟 실행 모델을 사용했습니다. 이후 제공자 내부의 모델 선택 문제를 제거하고 수집량을 줄이기 위해 서로 다른 제공자에 속한 네 모델로 범위를 축소했습니다.

## 데이터와 평가 방법

연구 방법은 실험이 진행되면서 다음과 같이 강화됐습니다.

### 작업 묶음과 분할

- 코드, 추론, 조사, 작문, 요약, 번역, 계획, 운영의 여덟 작업 종류를 사용했습니다.
- 같은 원본 작업에서 최대 호출 횟수만 바꾼 사례가 서로 다른 분할에 들어가지 않도록 scenario family 단위로 나눴습니다.
- task, split, prompt와 코드의 SHA-256을 manifest에 고정했습니다.
- Frontier-v4는 320개의 영어 scenario family를 train 192, validation 64, sealed holdout 64로 미리 나눴습니다.

### 실제 결과 수집과 채점

- 각 작업을 모든 후보 모델에 실행해 답변을 모았습니다.
- 어느 모델이 만든 답인지 가린 뒤 다른 모델들이 점수를 매겼습니다.
- 동일 제공자가 자신의 후보 답을 평가하지 못하게 제외했습니다.
- 점수는 채점자별 중앙값 편향을 보정한 뒤 중앙값으로 합쳤습니다.
- 코드·조사·요약·번역처럼 명시적인 계약이 있는 작업은 결정적 검증을 통과해야 라벨 후보가 될 수 있었습니다.
- 상위 두 후보의 점수 차이가 3점보다 작으면 라벨을 거부했습니다. 2점 이내의 품질 동률에서만 측정 지연 시간을 보조 기준으로 사용했습니다.
- 제공자별 금액 데이터가 비교 가능하지 않아 비용 가중치는 0으로 고정했습니다. 누락된 비용을 0원으로 취급하지 않았습니다.

### 평가 지표

| 지표 | 의미 |
| --- | --- |
| JSON/schema validity | 출력이 정해진 계약을 지켰는지 |
| Capability accuracy | 작업 종류를 올바르게 분류했는지 |
| Primary accuracy | 라벨에서 가장 좋은 모델을 첫 번째로 선택했는지 |
| Exact route | 주 실행·보조·검증 모델과 정책 필드 전체가 일치하는지 |
| Quality regret | 최고 점수와 선택한 모델 점수의 차이. 낮을수록 좋음 |

LLM 심사 점수는 실제 품질의 완전한 대용치가 아닙니다. 가능한 작업에는 결정적 검증을 추가했지만, 열린 형식의 작문·추론·계획은 여전히 모델 심사에 의존했습니다.

## 실험 연대기

### 1. Gemma 영어 JSON 계약 파일럿

첫 실험은 실제 모델 선택 능력보다 학습 파이프라인과 출력 계약을 확인하는 데 목적이 있었습니다.

- 데이터: 영어 합성 route 70 train / 10 validation / 20 test
- 선택 checkpoint: 80 steps, rank 8, alpha 16, dropout 0.05
- 학습 파라미터: 19.44M, 전체의 0.245%
- peak MLX memory: 8.72GB

| 지표 | Base | Adapter |
| --- | ---: | ---: |
| JSON parse | 100% | 100% |
| Schema valid | 0% | 100% |
| Decision accuracy | 0% | 90% |
| Target accuracy | 60% | 90% |
| Exact route | 0% | 70% |

이 결과는 Gemma가 구조화된 계약을 배울 수 있다는 증거였습니다. 하지만 라벨은 사람이 작성한 정책이었고 실제 후보 모델의 반사실적 품질, 속도나 비용을 측정하지 않았습니다. 마스킹을 고치는 과정에서 test split을 일부 확인했기 때문에 수치도 탐색 결과로만 취급했습니다.

### 2. 영어·한국어 여덟 모델 outcome 파일럿

두 번째 실험부터 실제 후보 답변과 blind judge 점수를 라벨에 사용했습니다. 영어·한국어 source task 16개를 세 예산으로 변형했고, 별도 holdout 8개에서 24 route를 평가했습니다.

| 24-route 탐색 holdout | Base | 40-step raw | 80-step balanced |
| --- | ---: | ---: | ---: |
| Schema valid | 0% | 95.8% | 100% |
| Capability accuracy | 0% | 95.8% | 100% |
| Primary accuracy | 8.3% | 0% | 8.3% |
| Exact route | 0% | 0% | 0% |

80-step adapter는 한국어 12 route를 포함한 작업 분류와 형식을 모두 맞췄지만, 모델 선택은 base보다 나아지지 않았습니다. source task가 16개뿐이었고, 당시 지연 시간은 작업별이 아니라 여덟 작업 묶음 단위였으며, 실행 가능한 테스트와 출처 검증도 부족했습니다.

### 3. 네 모델 Frontier-v1과 보수적 사전 정책

실행 대상을 네 제공자의 대표 모델로 줄였습니다. 이전 결과를 네 모델 점수로 다시 계산하자 불가능한 `low + cross_model` route 6개가 제외돼 42개의 warm-start route가 남았습니다.

- 16개 source scenario의 승자: Opus 8, Sol 7, Grok 1, Gemini 0
- 상위 두 모델 점수 차이가 2점 이하인 scenario: 8/16
- 이미 확인한 historical holdout 20 route에서 adapter primary/exact: 20% / 20%
- paired legacy-judge regret: 6.657

adapter는 Opus와 Sol만 선택해 Gemini와 Grok을 언제 써야 하는지 배우지 못했습니다. 공개 평가 자료와 과거 점수로 만든 사전 정책이 primary를 강제로 바꾸는 공격적 실험도 진행했습니다. 라벨 일치는 8.3%에서 20.8%로 올랐지만 regret은 2.452에서 3.571로 악화돼 폐기했습니다. 이후 사전 정책은 주 실행 모델을 바꾸지 않고 제공자 독립성과 검증 슬롯만 보수적으로 고치는 역할로 제한했습니다.

### 4. Qwen 3.5 9B LoRA 실행 가능성

Qwen 모델 5.6GB를 고정 revision으로 내려받아 20-route historical holdout에서 base 추론을 확인했습니다.

| 지표 | Qwen base |
| --- | ---: |
| JSON parse | 100% |
| Schema valid | 5% |
| Decision accuracy | 10% |
| Capability accuracy | 10% |
| Primary agreement | 30% |
| Exact route | 0% |

base 평가는 23.73초, 최대 resident memory는 5.62GB였고 process swap은 0이었습니다. 이 값은 해당 세션의 관측치이며 일반적인 성능 수치가 아닙니다.

rank 8 LoRA는 21.639M 파라미터를 학습 대상으로 잡았지만 첫 step 전에 `Primitive::vjp Not implemented for CustomKernel`로 실패했습니다. fused rotary module 8개를 끈 우회는 즉시 발생하던 오류를 넘겼으나, 2-step probe가 3분 넘게 progress report나 adapter를 만들지 못했고 system swap 사용량이 약 0.88GB 늘었습니다. 계획했던 20-step과 80-step은 실행하지 않았으며 Qwen adapter도 만들지 않았습니다.

이는 Qwen 3.5의 일반적인 학습 가능성을 부정하는 결과가 아닙니다. 고정된 MLX-VLM revision과 이 Mac에서 실용적으로 빠르고 미분 가능한 학습 경로를 찾지 못했다는 로컬 실행 가능성 결과입니다.

### 5. Frontier-v2 — task별 수집과 첫 sealed 평가

이전 약점을 보완해 `(task, model)`마다 별도 실행 시간과 usage를 기록하고, 결정적 검증과 3점 winner margin을 추가했습니다. 128개의 기본 task와 12개의 code recovery task를 만들었고, 그중 development/recovery task 108개의 실제 결과를 수집했습니다.

Standalone balanced Gemma adapter의 validation primary는 22.2%였습니다. Gemma가 capability와 schema를 맡고 train-only 경험 정책이 primary를 선택하는 hybrid는 다음 결과를 기록했습니다.

| Validation 지표 | Hybrid |
| --- | ---: |
| Primary accuracy | 59.7% |
| Exact route | 47.2% |
| Mean regret | 1.361 |

policy manifest와 80개의 정답 없는 예측을 먼저 봉인한 뒤 32-task holdout의 후보 128개와 심사 128개를 수집했습니다. 28개 task가 gate를 통과해 71/80 route가 채점됐습니다.

| 71-route sealed 결과 | Raw Gemma | Final hybrid |
| --- | ---: | ---: |
| Primary accuracy | 31.0% | 40.8% |
| Exact route | 정책 출력으로 별도 채점 안 함 | 33.8% |
| Mean regret | 4.089 | 3.404 |

다수 primary 기준선 32.4%는 넘었지만 영어 primary/exact는 58.1%였던 반면 한국어 primary는 27.5%, exact는 15.0%였습니다. reasoning primary는 0%, code는 25%였습니다. 전체 평균 개선이 작업 구간 전반의 개선으로 이어지지 않아 승격하지 않았습니다.

### 6. Frontier-v3 — 한국어 code/reasoning 재검증

Frontier-v2와 겹치지 않는 한국어 scenario family 80개를 code 20개와 reasoning 60개로 구성했습니다. 64개 development와 16개 sealed holdout으로 먼저 분할했습니다.

Capability-local balance 학습의 step 210 checkpoint는 validation에서 schema 100%, raw primary 38.5%, exact 26.9%를 기록했습니다. validation에서 만든 보정 정책을 적용하자 primary 61.5%, exact 42.3%로 올랐지만 regret 6.705는 static GPT의 5.641보다 나빴습니다.

동결 후 16-task sealed holdout 결과는 다음과 같았습니다.

| 지표 | Candidate | Static GPT |
| --- | ---: | ---: |
| Primary accuracy | 41.7% | 41.7% |
| Mean regret | 6.583 | 6.509 |

Candidate exact route는 30.6%였고 reasoning primary/exact는 25.0% / 8.3%였습니다. Primary는 동률이고 regret은 더 나빠 승격하지 않았습니다. 이 실패로 primary와 regret을 동시에 개선해야 한다는 규칙을 promotion gate에 추가했습니다.

### 7. Frontier-v4 — 열린 개발 세트의 희망과 독립 검증 실패

처음에는 기존 영어 validation 37 route에서 prompt-aligned 160-step Gemma 후보가 좋아 보였습니다.

| 열린 development validation | Candidate | Static GPT |
| --- | ---: | ---: |
| Primary accuracy | 51.4% | 40.5% |
| Mean fail-closed regret | 1.658 | 4.649 |
| Exact route | 45.9% | 해당 없음 |

이 수치는 이미 열린 37-route 개발 결과였으므로 sealed 성능으로 간주하지 않았습니다. 이후 영어 family 320개를 새로 만들고 development 256개와 미개봉 holdout 64개로 고정했습니다. 각 여덟 작업 종류에는 40개 family가 있고, train/validation/test는 24/8/8로 나뉩니다.

Development에서 후보 답변 1,024개와 blind judgment 1,024개를 수집했습니다. robust gate가 247/256 task를 채택해 train 463 route/185 scenario와 validation 154 route/62 scenario를 만들었습니다. 승자 분포는 GPT 94, Claude 74, Grok 70, Gemini 9로 불균형했습니다.

Observed와 capability/provider-capped 데이터로 학습한 일곱 checkpoint는 최종 route schema를 모두 지켰지만, 어느 것도 정적 GPT 기준선을 넘지 못했습니다.

| 후보 | Primary accuracy | Fail-closed regret |
| --- | ---: | ---: |
| Static GPT baseline | 55.2% | 1.945 |
| Observed step 80 | 50.6% | 2.643 |
| Observed step 160 | 37.7% | 3.097 |
| Observed step 240 | 40.9% | 2.701 |
| Observed step 320 | 44.2% | 3.951 |
| Capped step 80 | 46.8% | 4.302 |
| Capped step 160 | 23.4% | 7.724 |
| Capped step 240 | 51.9% | 3.633 |

Capped step 160은 validation 154 route에서 모두 Claude를 고르는 붕괴를 보였습니다. 일곱 후보 모두 primary, regret, 구간별 비회귀와 두 bootstrap 신뢰 관문을 통과하지 못했습니다. Adapter, policy와 정답 없는 예측을 동결하지 않았고 sealed holdout 64개도 열지 않았습니다.

### 8. Prompt-only Qwen 조정 실험

학습을 우회해 Qwen이 한 번에 하나의 `delegate` 또는 `finish` 행동만 내도록 만들었습니다. JSON Schema와 결정적 상태 기계는 다음을 provider 호출 전에 검사했습니다.

- 정해진 호출 횟수 안에서 답 작성과 필수 검증까지 끝낼 수 있는지
- 성공한 이전 단계만 참조하는지
- 역할과 사유 코드가 일치하는지
- 답을 만든 모델과 검증 모델이 서로 다른지
- 검증 결과가 `VERDICT: PASS` 또는 `VERDICT: FAIL`로 시작하는지
- 완료 행동이 올바른 답 단계와 검증 단계를 가리키는지

여덟 작업 종류의 development task에서 첫 행동은 8/8 parse·schema valid였고, 로컬 생성 시간은 행동당 0.78~1.48초였습니다.

실제 Orca 실행은 두 건이었습니다.

1. 최대 호출 횟수가 1인 writing task에서 Qwen이 Claude worker를 선택했습니다. Claude 호출은 12.394초에 성공했고 Qwen은 `step-1`을 답으로 완료했습니다.
2. 독립 검증이 필요한 code task에서 GPT thinker → Claude worker → Gemini verifier 순서를 선택했습니다. Gemini가 `VERDICT: PASS`를 반환했고 Qwen은 `step-2`를 답, `step-3`을 검증으로 지정해 완료했습니다.

두 실행은 배선과 제한 규칙이 작동한다는 증거입니다. 작업 품질, 모델 선택 우위, 평균 속도나 비용 효과를 평가하기에는 표본이 부족합니다. 이 경로를 승격하지 않았으며 Frontier-v4 holdout도 계속 봉인했습니다.

## 전체 결과에서 확인된 사실

- Gemma는 서로 다른 데이터 설계에서도 schema와 capability를 비교적 안정적으로 학습했습니다.
- 모델 선택 정확도는 validation과 holdout 사이에서 크게 흔들렸습니다.
- 열린 37-route validation에서 좋아 보였던 Frontier-v4 설계도 새 154-route validation에서는 정적 GPT를 넘지 못했습니다.
- 데이터 균형을 맞추면 특정 모델로의 붕괴가 사라지기도 했지만, 다른 checkpoint에서 다시 나타났습니다.
- Qwen prompt-only 방식은 정해진 계약 안의 여러 단계 호출을 수행할 수 있었습니다. 다만 평가한 실제 경로는 두 건뿐입니다.

## 실패 원인에 대한 해석

아래 내용은 측정값이 아니라 현재 근거에서 도출한 해석입니다.

### Hard winner label의 정보 손실

각 task에 정답 모델 하나만 남기면 모델 사이의 점수 차이와 심사 불확실성이 사라집니다. Frontier-v1의 16 scenario 중 8개가 2점 이하 차이였다는 사실은 작은 점수 변화만으로 라벨이 바뀔 수 있음을 보여 줍니다.

### Route 수와 독립 task 수의 차이

같은 scenario에 여러 최대 호출 횟수를 적용하면 route 수는 늘지만 새로운 문제 유형이 같은 비율로 늘지는 않습니다. Frontier-v4의 train 463 route도 독립 scenario로는 185개였습니다.

### 학습 목표와 실제 오케스트레이션의 불일치

QLoRA target은 한 번에 완성된 고정 JSON route였습니다. Completion loss의 많은 부분은 반복되는 JSON token을 예측하는 데 쓰였고, 실제 여러 단계 오케스트레이션에 필요한 대화, 검증 결과 해석과 실패 복구는 학습 대상이 아니었습니다. 결과적으로 이 접근은 여러 모델을 지휘하는 정책보다 정적 분류기에 가까웠습니다.

### 제공자 품질의 비정상성

클라우드 모델의 상대 품질은 task, prompt, 모델 버전과 서비스 상태에 따라 달라집니다. 작은 개인 데이터셋에서 한 시점의 winner를 학습해 장기간 일반화하기는 어려웠습니다.

이 해석들은 로컬 정책 모델의 원리적 한계를 증명하지 않습니다. 이번 데이터 표현과 objective가 목표 행동을 충분히 담지 못했다는 설명입니다.

## 타당성 한계

- 초기 합성 파일럿은 사람이 만든 정책 라벨을 사용했고 test split을 개발 중 일부 확인했습니다.
- 첫 outcome 실험은 source task가 16개뿐이었고 지연 시간이 task별로 측정되지 않았습니다.
- Frontier-v2와 v3의 sealed holdout은 각각 32개와 16개 source task로 작았습니다.
- 일부 결정적 검증은 형식과 명시된 test vector만 확인했으며 임의 코드를 실제로 실행하지 않았습니다.
- 조사 검증은 task 안에 제공된 합성 출처 ID를 확인했을 뿐 실제 웹 출처를 조회하지 않았습니다.
- 열린 형식의 작업은 세 독립 제공자의 LLM 심사에 의존했습니다. 사람 선호도와 동일하다고 볼 수 없습니다.
- 금액은 Claude만 일관되게 보고했습니다. 전체 제공자 비용을 비교할 수 없으므로 비용은 승격 판단에 넣지 않았습니다.
- Qwen prompt-only 경로는 실제 trajectory 두 건만 확인했습니다.
- 모든 결론은 48GB Apple Silicon Mac과 고정된 2026-07 소프트웨어·모델 조합에 한정됩니다.

## 중단 결정

로컬 학습 정책 모델과 prompt-only Qwen 조정 모델을 모두 활성 기본값으로 승격하지 않습니다.

Prompt-only 방식은 학습이 필요 없지만 그만큼 학습된 개인 정책의 이점도 없습니다. 대신 다음 운영 부담이 추가됩니다.

- 로컬 모델 로딩과 메모리 점유
- MLX와 MLX-VLM 호환성 관리
- 행동 계약, 재시도와 실패 모드 관리
- 작은 조정 모델이 작업을 잘못 나누거나 불필요한 유료 호출을 만들 위험

현재 구현 방향은 클라우드 모델이 계획을 제안하고 로컬 코드가 plan schema, 호출 한도, 단계 의존성, 독립 검증과 완료 조건을 강제하는 것입니다. 연구에서 만든 결정적 안전장치는 현재 Agent Hub의 adaptive orchestration에도 그대로 유효합니다.

## 보존할 자산

부정 결과와 무관하게 다음 자산은 다시 사용할 가치가 있습니다.

- 검토된 task bank와 split/hash manifest
- 중단 후 재개할 수 있는 Orca outcome collector
- blind judging과 동일 제공자 심사 제외
- 작업별 결정적 검증기
- 중앙값 기반 robust aggregation
- scenario-cluster bootstrap promotion gate
- model/data/policy/code를 묶는 provenance manifest
- label-free prediction을 먼저 동결하는 sealed holdout 절차
- coordinator action schema와 fail-closed state machine
- 최대 호출 횟수, 독립 검증, 명시적 verdict와 audit trace

Frontier-v4 sealed holdout 64개는 미개봉 상태로 보존합니다. 연구를 다시 시작하더라도 새 objective와 평가 단위가 달라지면 이 holdout을 억지로 재사용하지 않고 새 bank를 만드는 편이 타당합니다.

## 재개 조건

다음 중 적어도 하나가 실제 요구나 기술 조건으로 성립할 때만 로컬 정책 모델 연구를 다시 엽니다.

1. Qwen 3.5를 충분히 빠르게 학습할 수 있는 미분 가능한 MLX 경로가 제공됩니다.
2. 정적 winner JSON이 아니라 여러 단계의 대화·검증·복구 trajectory를 학습할 충분한 데이터와 reward가 준비됩니다.
3. 개인정보 보호, 완전한 offline 동작 또는 추가 API 호출 비용 0이 제품의 핵심 제약이 됩니다.
4. 클라우드 기준선보다 작업 품질, 지연 시간과 비용을 함께 개선할 가능성을 사전 선언한 development gate로 검증할 설계가 생깁니다.

재개할 때는 다음 순서를 지켜야 합니다.

1. 평가 단위와 기준선을 먼저 선언합니다.
2. 새 development/holdout task bank와 family split을 고정합니다.
3. prompt, 모델, adapter, state machine, policy code와 추론 설정을 manifest로 묶습니다.
4. 정답 없는 holdout 예측을 먼저 생성하고 hash를 고정합니다.
5. 그 뒤에만 holdout 결과를 수집하거나 라벨을 엽니다.

## 재현 상태

연구 구현 기준 커밋 `9d37b59`에서 기록된 검증 결과는 다음과 같습니다.

- `pytest`: 95 passed
- Python `compileall`: passed
- prompt dataset validation: 617 / 658 examples passed
- Ruler sync check: passed
- Phase 1 disposable fixture: passed
- `git diff --check`: passed

모델과 adapter, 원시 provider 응답, 상세 report 일부는 Git에서 제외된 로컬 artifact였습니다. Git에 남은 문서·manifest·코드·테스트는 연구 설계와 판정을 재구성할 수 있지만, 모든 세션 결과를 byte-for-byte 다시 만드는 완전한 공개 재현 패키지는 아닙니다.

## 근거 문서 지도

아래 경로는 보존된 `Chanwoo-act/agent-hub` 체크아웃 기준입니다.

| 근거 경로 | 내용 |
| --- | --- |
| `model-access/policy/gemma4-e4b-router/RESEARCH-CLOSURE.md` | 최종 결론, 재개 조건과 연구 경계 |
| `model-access/policy/gemma4-e4b-router/RUN-REPORT.md` | 전체 실험 연대기, 학습 설정과 측정값 |
| `model-access/policy/gemma4-e4b-router/QWEN35-ATTEMPT.md` | Qwen base 평가와 LoRA 실행 가능성 실패 |
| `model-access/policy/gemma4-e4b-router/QWEN-PROMPT-COORDINATOR.md` | prompt-only action 계약과 실제 Orca 실행 두 건 |
| `model-access/policy/gemma4-e4b-router/FRONTIER-V4-PLAN.md` | 영어 독립 평가 계획과 승격 조건 |
| `model-access/policy/gemma4-e4b-router/FRONTIER-V4-TASK-BANK-REVIEW.md` | 320-family task bank 검토와 verifier 경계 |
| `model-access/policy/gemma4-e4b-router/MODEL*.lock` | 모델, revision, 라이선스와 trainer 고정 정보 |
| `model-access/policy/gemma4-e4b-router/data/outcome_tasks_frontier_v4_en_manifest.json` | task 분포, hash, 중복·유사도 감사와 holdout 상태 |
| `model-access/policy/gemma4-e4b-router/configs/frontier-v4-en-collection-policy.json` | 심사 제외, 집계, 검증, 비용·지연 시간과 bootstrap 정책 |
| `model-access/policy/gemma4-e4b-router/scripts/check_promotion_gate.py` | fail-closed regret, 구간별 회귀와 승격 판정 구현 |
| `model-access/policy/gemma4-e4b-router/tests/test_promotion_gate.py` | primary·regret·schema·bootstrap 관문의 회귀 테스트 |

이 문서의 수치는 위 자료와 보존 저장소의 Git 이력을 대조해 작성했습니다. 해석이 근거 문서와 충돌하면 원시 manifest와 실행 report를 우선합니다.
