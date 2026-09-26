# 模型 Tier 由成本與 benchmark 計算

日期：2026-09-26
範圍：limit-usage（本 repo，含 `routing-policy/`）＋ skill `orchestrating-development`（`~/.agents/skills/orchestrating-development`，本機 git repo）

## 目標

1. skill 內不出現任何具體模型名稱。模型會下架，選模完全交給 limit-usage 的 API。
2. 每個模型能接哪些 Tier，不再人工填寫，而是由它的 API 牌價與 benchmark 算出。
3. 模型下架時自動停止派給它。

## 非目標

- 不自動抓 benchmark 或牌價；兩者仍人工寫進 catalog，只是 Tier 由程式計算。
- 不改 P2 的任務評分（A/B/C/D 軸 → T0–T3）。
- 不改即時額度的計算（score、level、pace penalty、pool cooldown）。
- 不改 routing-policy 的 API 路徑與認證。

## Decisions

| # | 決策 | 內容 | 來源 |
|---|---|---|---|
| D1 | 成本的定義 | API 公開牌價，輸入／輸出 $/MTok，以 1:3 加權成混合價 | 使用者 Q1=A |
| D2 | Tier 公式 | bench 決定最高 Tier、混合價決定最低 Tier，每個模型得到 [最低, 最高] 區間 | 使用者 Q2=A |
| D3 | API 不通時的兜底 | 本機快取最後一次的候選清單；連快取都沒有就依廠商順序、不帶 `--model` | 使用者 Q3=A |
| D4 | 下架偵測 | 主機端定時匯出各 CLI 可用模型清單＋派工遇「模型不存在」回報 404 | 使用者 Q4=A |
| D5 | skill 版本控制 | skill 目錄 `git init`（純本機），在自己的 worktree 實作與審查，核准後 fast-forward | 使用者 Q5=A |

### 編排者裁定

| # | 裁定 | 理由 |
|---|---|---|
| R1 | 模型資料與 Tier 公式放在 repo 根目錄的 `catalog/` 套件，limit-usage 與 routing-policy 共用 | 目前兩份模型表靠 parity 測試同步；公式只能有一份 |
| R2 | routing-policy 的 build context 改為 repo 根目錄 | 才能 `COPY catalog/` |
| R3 | 移除 `max_tier`／`cost_rank`／`role`／`reviewer` 的人工欄位，全部由公式推導 | D2 |
| R4 | 排序鍵維持「eligible → usable → score ↓ → bench ↓」，最後的 `cost_rank` 換成混合價 ↑ | 不動現有額度優先行為 |
| R5 | 審查者 = 區間含 T3 且可派工的模型；編排者 = 最高 Tier 為 T3 且廠商不在 `orchestrator_excluded_vendors`；全分支審查 = 可用審查者中 bench 最高者 | 目前三者的結果完全重現 |
| R6 | effort 移進 catalog，由 API 回傳；skill 的 `effort_for_model` 刪除；`dispatch` 對 claude 也傳 `--effort` | effort 綁模型，屬於 catalog；`claude` CLI 支援 `--effort` |
| R7 | 下架以「模型」為單位標記，不動廠商池的 cooldown | 現行 404 會讓整個廠商池停 5 分鐘 |
| R8 | CLI 有列出、catalog 未收錄的模型，在 API 的 `catalog.uncatalogued` 回報 | 例：`grok-4.7` 已是 grok 預設但不在 catalog |
| R9 | skill 的 selftest 用虛構模型名稱，並新增一條檢查：skill 內不得出現具體模型 id | 規則要能被強制，不只寫在文件裡 |
| R10 | 門檻數值見下方〈公式〉；以 2026-09-26 的資料，算出的最高 Tier 與目前人工 `max_tier` 完全相同 | 讓行為變化只來自新的「最低 Tier」 |

## 架構

```
catalog/                          ← 新，repo 根目錄，唯一來源
  models.yaml                     模型資料（pool、slots、bench、price、effort、dispatchable）＋ pools
  tiering.yaml                    公式參數（混合權重、各 Tier 門檻、排除廠商）
  tiering.py                      載入、驗證、推導（純函式）

limit-usage (app/)                GET /api/routing 由 catalog 推導 candidates；讀 data/cli-models.json；每模型下架標記
routing-policy (routing-policy/)  engine 由 catalog 推導 candidates；signals 另讀 models.<id>.usable；回應帶 effort
deploy/cli-models-export.py       主機端，每 6 小時寫 data/cli-models.json
skill                             不含模型名稱；effort 取自 API；兜底改為快取 → 廠商預設
```

## catalog 契約

### `catalog/models.yaml`

```yaml
schema_version: 2
catalog_version: "2026-09-26.1"
pools:            # 內容原樣搬自 routing-policy/policy/models.yaml 的 pools
  claude: {provider: claude, display_name: "Claude Code (claude)", slots: {"5h": "5h", "1w": "1w", "1w-fable": "1w-fable"}}
  ...
models:
  <model-id>:
    pool: <pool id>                 # 必填，須存在於 pools
    slots: [<slot>, ...]            # 必填，須是該 pool 的 slots key
    bench: <int 0–100>              # 必填，綜合 coding benchmark 指數
    bench_note: <str>               # 選填，依據摘要
    price:                          # 必填
      input: <float ≥ 0>            # USD / 1M input tokens
      output: <float ≥ 0>           # USD / 1M output tokens
      as_of: "YYYY-MM-DD"
      source: <url>
      proxy_of: <model-id>          # 選填：沒有公開 API 價時，借用哪個模型的價格
    effort: <str|null>              # 選填，預設 null。傳給 CLI 的推理強度
    dispatchable: <bool>            # 選填，預設 true
```

未列出的 key 一律拒絕載入。

### `catalog/tiering.yaml`

```yaml
schema_version: 1
blend: {input: 1, output: 3}
tiers:
  T0: {min_bench: 0,  max_blended_price: 5}
  T1: {min_bench: 60, max_blended_price: 10}
  T2: {min_bench: 74, max_blended_price: 25}
  T3: {min_bench: 86, max_blended_price: null}   # null = 不設上限
orchestrator_excluded_vendors: [agy]
```

驗證：`min_bench` 隨 Tier 遞增（非遞減）、`max_blended_price` 隨 Tier 遞增（null 視為無限大）、T0 的 `min_bench` 必須為 0。

### 公式（`catalog/tiering.py`）

- `blended_price = (blend.input × price.input + blend.output × price.output) / (blend.input + blend.output)`，四捨五入到小數 4 位
- `max_tier` = 滿足 `bench ≥ min_bench` 的最高 Tier
- `min_tier` = 滿足 `max_blended_price is null or blended_price ≤ max_blended_price` 的最低 Tier
- `tiers` = `[min_tier … max_tier]`；`min_tier > max_tier` 時為空（此模型不接任何實作）
- `vendor` = pool id 第一個 `-` 之前的部分（`agy-3p` → `agy`）
- `reviewer` = `dispatchable and "T3" in tiers`
- `orchestrator` = `max_tier == "T3" and vendor not in orchestrator_excluded_vendors`
- `role` = `"orchestrator"` if orchestrator else `"subagent"`（保留給既有使用者）

公開介面：

```python
@dataclass(frozen=True)
class Derived:
    model: str; vendor: str; pool: str; slots: tuple[str, ...]
    bench: int; blended_price: float; effort: str | None; dispatchable: bool
    min_tier: str | None; max_tier: str; tiers: tuple[str, ...]
    reviewer: bool; orchestrator: bool; role: str

@dataclass(frozen=True)
class Catalog:
    catalog_version: str
    pools: Mapping[str, Mapping[str, Any]]
    models: Mapping[str, Derived]          # 依 catalog 檔案順序
    tier_ids: tuple[str, ...]              # ("T0","T1","T2","T3")
    blend: Mapping[str, float]
    thresholds: Mapping[str, Mapping[str, Any]]

def load_catalog(directory: str | Path | None = None) -> Catalog
    # 預設目錄：環境變數 CATALOG_DIR，否則本檔所在目錄
    # 任何驗證錯誤 → ValueError("<file>: <key.path>: <訊息>")
def tier_candidates(catalog: Catalog, tier: str) -> list[str]
    # tier ∈ T0–T3：dispatchable 且 tier in tiers；tier == "review"：reviewer
    # 排序：blended_price ↑，同價再依 model id ↑
```

### 以 2026-09-26 資料推導的結果（golden table，測試必須完全吻合）

| model | bench | 牌價 in/out | 混合價 | 區間 | reviewer | orchestrator | effort |
|---|---|---|---|---|---|---|---|
| gemini-3.8-flash-low | 55 | 0.75 / 3.75 | 3.0 | T0 | – | – | null |
| gemini-3.8-flash-medium | 70 | 0.75 / 3.75 | 3.0 | T0–T1 | – | – | null |
| gemini-3.8-flash-high | 81 | 0.75 / 3.75 | 3.0 | T0–T2 | – | – | null |
| gemini-3.1-pro-low | 65 | 2 / 12 | 9.5 | T1 | – | – | null |
| gemini-3.1-pro-high | 74 | 2 / 12 | 9.5 | T1–T2 | – | – | null |
| gpt-reserve | 50 | 0.2 / 1.2（proxy_of gpt-5.6-luna） | 0.95 | T0 | – | – | null |
| gpt-5.6-luna | 78 | 0.2 / 1.2 | 0.95 | T0–T2 | – | – | max |
| gpt-5.5 | 72 | 5 / 30 | 23.75 | （空） | – | – | null |
| gpt-5.6-terra | 82 | 2 / 12 | 9.5 | T1–T2 | – | – | high |
| gpt-5.6-sol | 90 | 4 / 20 | 16.0 | T2–T3 | ✓ | ✓ | max |
| grok-4.5 | 77 | 2 / 6 | 5.0 | T0–T2 | – | – | medium |
| grok-4.6 | 86 | 2 / 6 | 5.0 | T0–T3 | ✓ | ✓ | high |
| claude-haiku-4-5-20251001 | 60 | 1 / 5 | 4.0 | T0–T1 | – | – | null |
| claude-sonnet-5 | 80 | 2 / 10 | 8.0 | T1–T2 | – | – | null |
| claude-opus-5 | 92 | 5 / 25 | 20.0 | T2–T3 | ✓ | ✓ | null |
| claude-fable-5-1（dispatchable: false） | 95 | 10 / 50 | 40.0 | T3 | – | ✓ | null |

牌價來源（皆 2026-09-26 取得）：OpenAI `https://developers.openai.com/api/docs/pricing`（sol 為促銷價，至少到 2026-11-21）、xAI `https://docs.x.ai/docs/models/grok-4.6`／`grok-4.5`、Google `https://ai.google.dev/gemini-api/docs/pricing`、Anthropic `https://platform.claude.com/docs/en/about-claude/pricing`。bench 與 `bench_note` 沿用 `routing_view.py` 現值與 skill `references/routing.md` 本機模型總表的「依據」欄。effort 沿用現行 `effort_for_model`。

與現況的行為差異：T0 候選少了 sol、opus、terra、sonnet、pro-low、pro-high、gpt-5.5；T1 少了 sol、opus、gpt-5.5；T2、T3 不變；審查者、編排者集合不變。gpt-5.5 不再接任何 Tier。

## 下架偵測

### `data/cli-models.json`（主機端匯出程式寫、limit-usage 讀）

```json
{
  "generated_at": "2026-09-26T08:00:00Z",
  "vendors": {
    "codex": {"ok": true,  "models": ["gpt-reserve", "gpt-5.6-sol", "..."]},
    "agy":   {"ok": true,  "models": ["gemini-3.8-flash-high", "..."]},
    "grok":  {"ok": false, "error": "timeout after 60s"}
  }
}
```

### 匯出程式 `deploy/cli-models-export.py`（主機端，Python 3 stdlib only）

- codex：讀 `~/.codex/models_cache.json` 的 `models[].slug`（不分 visibility）
- agy：執行 `agy models`（timeout 60 s），取每行第一個 tab 之前的欄位；略過不含 tab 的行（例如 `Fetching available models...`）
- grok：執行 `grok models`（timeout 60 s），取 `Available models:` 之後、符合 `^\s*[*-]\s+(\S+)` 的第一個群組
- claude：沒有清單指令，不寫入
- 任一廠商失敗（找不到指令、非零離開、timeout、解析結果為空）→ `{"ok": false, "error": "<一行>"}`，不影響其他廠商
- 輸出路徑：第一個參數，預設 `~/Container/limit-usage/data/cli-models.json`；先寫同目錄暫存檔再 `os.replace`
- `deploy/limit-usage-cli-models.service`（oneshot）＋ `deploy/limit-usage-cli-models.timer`（`OnBootSec=2min`、`OnUnitActiveSec=6h`），寫法比照 `limit-usage-claude-monitor.*`

### limit-usage 讀取

- 路徑：設定 `cli_models_path`（env `CLI_MODELS_PATH`，預設 `/app/data/cli-models.json`；審查後決定 `config.py` 預設維持 `./data/cli-models.json`，由 docker-compose 設 `/app/data/cli-models.json`）；依 mtime 快取
- 某廠商清單「有效」= 檔案存在、可解析、`generated_at` 距今 ≤ 48 小時、該廠商 `ok: true`
- 每個模型 `listed`：廠商清單有效時為 `true`／`false`，否則 `null`
- `listed == false` → 該模型 `usable = false`
- `catalog.uncatalogued` = 有效清單中出現、catalog 未收錄的 id，依廠商分組；略過 agy 的非 `gemini-` id（agy 只用 gemini）

### 派工失敗回報

- skill `classify_failure`：trace 符合 `model not found|unknown model|no such model|invalid model|model .*(does not exist|is not available|not supported)`（不分大小寫）→ status `404`；此判斷優先於 429／401
- limit-usage `POST /api/routing/feedback`：`status == 404` 且帶 `model` 時，只把**該模型**標為下架 24 小時（`kind: "model_missing"`），不動廠商池 cooldown；同一模型之後回報 `ok: true` 即清除
- 被標記的模型 `usable = false`
- `GET /api/routing/feedback` 增加 `models: {<id>: {missing: true, until: <iso>, last_error: <str>}}`
- 標記存在記憶體（與現行 cooldown 相同），重啟即清除

## API 變更

### limit-usage `GET /api/routing`

`models.<id>`：
- 移除 `cost_rank`
- `max_tier`、`role` 改為推導值
- 新增 `min_tier`、`tiers`、`blended_price`、`effort`、`reviewer`、`orchestrator`、`listed`、`missing`（下架標記 view 或 null）

`tiers.<T>.candidates[]`：移除 `cost_rank`；新增 `min_tier`、`blended_price`、`effort`

新增頂層 `catalog`：`{catalog_version, blend, thresholds, uncatalogued: {vendor: [id]}, cli_models: {generated_at, vendors: {vendor: {ok, fresh, count}}}}`

`orchestrators`：改用推導的 `orchestrator`；排除 `orchestrator_excluded_vendors`

`?model=X`、`?tier=T`、濾器參數、`wait_seconds` 的語意不變。

### routing-policy

- `policy/models.yaml` 刪除，改讀 `catalog/`（容器內 `/app/catalog`）
- `policy/tiers.yaml` 保留（`complexity`、`policy_version` → `"2026-09-26.1"`）
- `Policy` 的模型與 candidates 改由 `catalog.tiering` 推導；`roles.yaml` 的 `orchestrator.excluded_vendors` 移到 `tiering.yaml`
- `signals` 除 `pools` 外，另讀 `models.<id>.usable`（limit-usage 已含 `listed` 與下架標記），engine 排除 `usable == false` 的模型
- `/v1/recommend` 回應：`recommended` 與 `alternatives[]` 每項新增 `effort: str | null`
- `/v1/policy` 仍不得洩漏模型 id、bench、價格
- docker-compose：`routing-policy.build` 改為 `{context: ., dockerfile: routing-policy/Dockerfile}`；Dockerfile 的 COPY 路徑相應調整並加 `COPY catalog ./catalog`
- limit-usage Dockerfile 加 `COPY catalog ./catalog` 與 `RUN pip install --no-cache-dir 'PyYAML>=6'`；`requirements.txt` 加 `PyYAML>=6`

## skill 變更

- `scripts/lib/routing.sh`：刪除 `route_implement`、`route_review`、`route_review_avoid`、`effort_for_model` 與所有模型 id
  - `EFFORT` 取自 API：`/api/routing` 為被選中 candidate 的 `.effort`；central policy 為 `.recommended.effort`；central policy 快取一併存 `effort`
  - 每次 `route_quota` 成功，把該次 `?tier=<T>` 回應的 `candidates` 存進 `~/.cache/subagent-routing/candidates.json`（`entries["<tier>"] = {fetched_at, candidates}`，`chmod 600`，原子寫入）
  - API 不通或沒有合格候選時：(1) 讀快取該 tier 的 candidates，套用 avoid／vendors 濾器，取 `bench` 最高者，stderr 印 `dispatch: routing API unavailable; cached candidates (<age>s old) -> <vendor> <model>`；(2) 無快取 → 依 `DISPATCH_ROUTING_FALLBACK_VENDORS`（預設 `codex,claude,grok`）取第一個未被 avoid、且在 `DISPATCH_ROUTING_VENDORS` 允許內的廠商，`MODEL=`、`EFFORT=`，stderr 印 `dispatch: no routing data; <vendor> with its default model`
  - 全分支審查：`tiers.review` 中 eligible 者取 `bench` 最高；兜底同上（tier 鍵為 `review`）
- `scripts/dispatch`：
  - `MODEL` 為空時四家 CLI 都不帶 `--model`／`-m`
  - claude 在 `EFFORT` 非空時加 `--effort "$EFFORT"`
  - `MODEL` 為空時不送 feedback
  - agy 且 `MODEL` 為空 → `die "agy needs an explicit gemini-* model"`
- `scripts/route`：
  - orchestrator 兜底改為廠商預設（無模型）
  - 改用 `routing_curl`
  - 新增 `--list`：印出 `/api/routing` 的 `models`，欄位 `model vendor bench blended tiers effort usable listed`，並列出 `catalog.uncatalogued`
- `scripts/selftest`：
  - stub 改用虛構 id（`model-strong`、`model-mid`、`model-cheap`、`model-review`），補 T0、T3 與無 tier 的完整 payload，candidates 帶 `effort`
  - 新增案例：快取兜底、廠商預設兜底、claude `--effort`、空 `MODEL` 不帶 `--model`、`classify_failure` 404
  - 新增「skill 無具體模型名稱」檢查：對 `SKILL.md references/ scripts/`（排除 `scripts/selftest` 本身）執行 `grep -rEn '\b(claude|gpt|grok|gemini)-[a-z]*-?[0-9]|\b(Opus|Sonnet|Haiku|Fable|Mythos|GPT-?|Grok|Gemini) ?[0-9]'`，須無結果。`gemini-*`、`claude-*` 這類 glob 不含數字，不受影響
- `references/routing.md`：刪除本機模型總表、可用模型清單、編排者模型表、審查者名稱、第 2 輪兜底表中的模型 id、靜態兜底表；改為說明 catalog 位置、公式、`route --list`、兜底順序
- `SKILL.md`：〈參考〉段落同步描述

## 錯誤處理

- catalog 驗證失敗：limit-usage 與 routing-policy 啟動時直接失敗（fail fast），訊息含檔名與 key 路徑
- `cli-models.json` 缺失、損毀、過期：不過濾（`listed = null`），不影響啟動
- skill 快取檔損毀：當作無快取

## 測試策略

- `tests/test_catalog.py`：golden table 逐欄吻合；每條驗證規則各一個失敗案例；`tier_candidates` 排序
- limit-usage 既有 `tests/test_routing_view.py`、`tests/test_routing_feedback.py`：改用 `tmp_path` 內的虛構 catalog fixture，斷言不再引用真實模型 id；另保留一個用真實 catalog 的煙霧測試（載入成功、T3 集合 = {sol, grok-4.6, opus-5}）
- 新測試：`listed` 過濾與 48 小時新鮮度、`uncatalogued`、404 模型下架標記（不影響同池其他模型、成功回報清除、24 小時到期）
- `tests/test_cli_models_export.py`：以 fixture 字串測三種解析、單一廠商失敗隔離、原子寫入
- `routing-policy/tests/`：parity 測試改為兩邊都讀同一份 catalog；engine 排除 `usable == false`；`/v1/recommend` 帶 `effort`；`/v1/policy` 不洩漏
- skill `scripts/selftest` 全數通過

## 上線順序

1. limit-usage PR 合併 → 重建兩個 image 並重啟容器 → 安裝 cli-models timer
2. skill fast-forward

skill 新版在舊 API 上會少 effort（CLI 用預設 effort）但仍可派工；舊 skill 在新 API 上不受影響。

## Global Constraints（推導所得：本 repo 無 AGENTS.md／CLAUDE.md）

- commit 訊息：conventional commits，主旨繁體中文，例：`feat(routing-policy): …`、`fix(claude): …`
- Python：limit-usage 在 3.12 容器、routing-policy 在 3.14 容器；測試 `pytest`（repo 根目錄 `tests/`，`routing-policy/` 內 `tests/`）
- routing-policy 的 pydantic 模型維持 `extra="forbid", strict=True, frozen=True`
- 對外 port 只綁 `127.0.0.1`（docker-compose 註解明訂）
- 路由永遠 best-effort：任何路由失敗都不得讓 dispatch 失敗（skill `lib/routing.sh` 標頭明訂）
- skill 的 shell 在 `set -u` 下執行，需相容 bash ≥ 4
