# 計畫 — Routing Policy Service（架構報告 PR1–PR4）

規格來源：使用者提供的《limit-usage 瘦身、OmniRoute 整合與跨網路 Subagent Routing 架構規劃報告》(2026-09-15)，
存於 `docs/dev/specs/2026-09-15-routing-policy-architecture.md`。

## 範圍判定（編排者裁定）

| 報告 PR | 狀態 | 理由 |
|---|---|---|
| PR1 政策抽出 | 本計畫 Task 1–2 | |
| PR2 Policy API | 本計畫 Task 3–4 | |
| PR3 外網曝光 | **不在本計畫**（D=3，需使用者授權網域與 CF token） | |
| PR4 Skill 整合 | 本計畫 Task 5 | |
| PR5–PR9 | **阻擋** | OmniRoute 未部署在本機（`docker ps` 只有 `cli-proxy-api`）；parity 測試無對象 |

## 編排者裁定（技術決定，不回頭問使用者）

1. **routing-policy 放在 limit-usage repo 的 `routing-policy/` 子目錄**。報告 §34 明言第一階段不搬 repository。
2. **Phase 1 的 live signal 來源是 limit-usage `GET /api/routing`**，不是 OmniRoute（未部署）。報告 §43 就是這個資料流。
3. **limit-usage 本次不動**：`/api/routing`、`routing_view.py`、`routing_feedback.py` 全部保持原樣。報告 §47「先完成 responsibility extraction，再改演算法」；§53 的退場要等所有 Skill 都改走新 API 之後。因此本計畫**只新增，不刪除**。
4. **policy_version 用 `<UTC 日期>.<當日序號>` 形式**，由 policy YAML 內的欄位提供（報告 §40 的第一種形式）；不自動從 git SHA 推導，因為容器內不保證有 .git。
5. **行為 parity 的定義**：對同一組 pool 分數與 cooldown 輸入，`/v1/recommend` 的 `recommended` 與候選排序必須與 `build_routing_payload` 的 `tiers[tier]` 一致。這是 Task 2 的驗收測試。

## Global Constraints（本 repo 推導所得，無 AGENTS.md/CLAUDE.md）

- Python 3.14、FastAPI + pydantic-settings、`from __future__ import annotations` 開頭、型別註記完整。
- 測試在 `tests/`，`pytest.ini` 已設定；純函式測試不得需要網路。
- 模組 docstring 用完整段落說明「為什麼這樣設計」，風格見 `app/services/routing_view.py`、`app/services/routing_feedback.py`。行內註解寫決策理由，不複述程式碼。
- 服務預設 bind `127.0.0.1`；compose 的 port 只發佈到明確位址，不用 `0.0.0.0`（compose 現有註解說明原因：published port 繞過 ufw）。
- commit message 用 `feat(scope): 中文描述` 形式，見 `git log`。
- 不得在任何回應或日誌輸出 OAuth token、API key、帳號 email、credential 路徑。

---

### Task 1: policy 設定檔與載入器
- **檔案**: `routing-policy/policy/models.yaml`、`routing-policy/policy/tiers.yaml`、`routing-policy/policy/roles.yaml`、`routing-policy/app/policy.py`、`routing-policy/tests/test_policy_loader.py`
- **要做什麼**:
  - 把 `app/services/routing_view.py` 的 `POOLS`、`MODELS`、`TIER_LEVELS` 逐筆搬成 YAML，**數值一字不改**。
  - `models.yaml`: 每個 model 的 `pool`、`slots`、`max_tier`(0–3 整數)、`bench`、`cost_rank`、`role`、`reviewer`(bool, 預設 false)、`dispatchable`(bool, 預設 true)。pool 定義同檔或 `roles.yaml` 皆可，但 `pool` → `{provider, display_name, slots}` 必須完整保留 `agy` / `agy-3p` 兩個池。
  - `tiers.yaml`: `schema_version: 1`、`policy_version: "2026-09-15.1"`、T0–T3 的 `complexity` 區間 `[0,2] [3,5] [6,8] [9,12]`、`review` tier（role=review，候選=reviewer 模型）。
  - `roles.yaml`: `policy.review.cross_vendor: true`、`ranking: [availability, quota, capability, cost]`、`orchestrator` 不得為 `agy` 廠商的規則。
  - `app/policy.py`: `load_policy(path) -> Policy`（dataclass 或 pydantic model），欄位驗證失敗要拋出帶檔名與欄位名的明確錯誤；提供 `Policy.tier_candidates(tier_id) -> list[str]`，語意與 `routing_view.tier_candidates` 完全相同（review 只收 reviewer；其餘收 `max_tier >= level and dispatchable`；皆依 `cost_rank` 排序）。
- **驗證**: `cd routing-policy && python -m pytest tests/test_policy_loader.py -v`。測試必須包含一個 parity 測試：`sys.path` 加入 repo 根目錄後 `from app.services.routing_view import MODELS, POOLS, TIER_LEVELS, tier_candidates`，逐欄位斷言 YAML 載入結果與之相同，且每個 tier 的 `tier_candidates` 清單順序相同。
- **依賴**: 無
- **評分**: A=2 B=1 C=0 D=0 → 總分 3
- **Tier**: T1
- **指派**: 依 `/api/routing` 即時額度
- **審查**: 編排者讀 diff（T1 不派交叉審查）

### Task 2: 推薦引擎（純函式）
- **檔案**: `routing-policy/app/engine.py`、`routing-policy/tests/test_engine.py`
- **要做什麼**:
  - `recommend(policy, signals, *, tier, role, implemented_by_vendor=None, available_vendors=None, min_score=None, now=None) -> Recommendation`。
  - `signals` 形狀：`{pool_id: {"usable": bool, "score": float|None, "level": str, "cooling": bool, "seconds_until_reset": int|None, "stale": bool}}`。純資料，引擎不發任何網路請求。
  - 排序鍵與 `routing_view.build_routing_payload` 完全相同：`(not eligible, not usable, -score, -bench, cost_rank)`，`score is None` 視為 `-1.0`。
  - `role="review"` 時：候選限 `reviewer: true` 的模型；若 policy 的 `review.cross_vendor` 為 true 且給了 `implemented_by_vendor`，排除該 vendor（等同 `avoid_vendor`）。
  - `available_vendors` 等同 `routing_view` 的 `vendors` 過濾；`min_score` 等同其 `min_score`。
  - 回傳 `recommended: {vendor, model} | None`、`alternatives: [{vendor, model}, ...]`（eligible 中排第 2 名起，最多 3 筆）、`reason_codes: list[str]`、`wait_seconds: int|None`。
  - `reason_codes` 取值固定為這些字串：`tier_capable`、`quota_healthy`、`quota_degraded`、`provider_healthy`、`cross_vendor_review`、`vendor_filtered`、`stale_signals`、`no_eligible_candidate`、`fallback_static`。
  - **不得**回傳原始 quota 百分比、pool 內部欄位、cooldown 明細（報告 §22/§23）。`quota_state` 若要回傳只能是 `healthy|degraded|unknown`。
- **驗證**: `cd routing-policy && python -m pytest tests/test_engine.py -v`。必要案例：(a) 全部健康時 T0–T3 各自選出分數最高者；(b) 分數相同時選 bench 較高者；(c) bench 也相同時選 cost_rank 較低者；(d) review + `implemented_by_vendor="codex"` 不得回傳 codex 模型；(e) 無 eligible 時 `recommended` 為 None 且 `reason_codes` 含 `no_eligible_candidate`、`wait_seconds` 為候選中最小者；(f) `claude-fable-5-1` 永不出現在任何 implement tier 的候選中；(g) 一個 parity 測試：用同一組 signals 餵給本引擎與 `build_routing_payload`（後者用合成 snapshot），斷言每個 tier 的 `recommended` 相同。
- **依賴**: Task 1
- **評分**: A=1 B=2 C=1 D=0 → 總分 4
- **Tier**: T1
- **指派**: 依 `/api/routing` 即時額度
- **審查**: 編排者讀 diff

### Task 3: limit-usage signal adapter
- **檔案**: `routing-policy/app/signals.py`、`routing-policy/tests/test_signals.py`
- **要做什麼**:
  - `SignalSource(url, timeout, ttl_seconds)`：`async fetch() -> Signals`，打 `GET {url}`（預設 `http://127.0.0.1:50048/api/routing`），把回應的 `pools` 投影成 Task 2 要的 signals 形狀。只取 `usable`/`score`/`level`/`cooldown.cooling`/`stale`，以及各 pool 綁定窗的 `seconds_until_reset`（取 `windows` 中 `binding_slot` 那一個）。
  - TTL 快取（預設 30 秒）避免每次 recommend 都打 limit-usage。
  - 上游不可用（連線失敗、非 200、JSON 壞掉、逾時）時**不得拋例外**：回傳最後一次成功的 signals 並標記 `stale=True`；從未成功過則回傳空 signals 並標記 `degraded`。
  - 記錄一行 log，內容只有 url host、狀態碼、耗時；**不得**記錄回應內容。
- **驗證**: `cd routing-policy && python -m pytest tests/test_signals.py -v`。用 `httpx.MockTransport` 或 monkeypatch，不得起真的 server。案例：正常投影、TTL 內不重打、上游 500 沿用舊快取並標 stale、首次即失敗回 degraded。
- **依賴**: Task 1
- **評分**: A=1 B=2 C=1 D=0 → 總分 4
- **Tier**: T1
- **指派**: 依 `/api/routing` 即時額度
- **審查**: 編排者讀 diff

### Task 4: FastAPI 服務與部署
- **檔案**: `routing-policy/app/main.py`、`routing-policy/app/config.py`、`routing-policy/app/__init__.py`、`routing-policy/requirements.txt`、`routing-policy/Dockerfile`、`routing-policy/pytest.ini`、`routing-policy/README.md`、`routing-policy/tests/test_api.py`、根目錄 `docker-compose.yml`（新增 service）
- **要做什麼**:
  - `GET /v1/health` → `{"status":"ok","policy_version":"..."}`，**只有這兩個欄位**（報告 §56）。
  - `GET /v1/policy` → `{schema_version, policy_version, generated_at, tiers:{T0..T3:{complexity:[lo,hi]}}, review:{cross_vendor}}`。不得輸出 models 明細、pool 名稱、provider 名稱。
  - `POST /v1/recommend`，request body：`{tier, role, implemented_by_vendor?, client:{available_vendors?}?, min_score?}`；`tier` 限 `T0|T1|T2|T3`，`role` 限 `implement|review|orchestrate`，其他值回 422。
    Response：`{policy_version, generated_at, expires_at, tier, role, recommended:{vendor,model}|null, alternatives:[...], reason_codes:[...], wait_seconds}`。`expires_at = generated_at + RECOMMEND_TTL_SECONDS`（預設 300）。
  - `role="orchestrate"`：候選限 `role: orchestrator` 的模型，且排除 `dispatchable: false` 以外——`claude-fable-5-1` 在 orchestrate 可以出現（它本來就是編排者），實作 tier 不可。
  - 設定用 pydantic-settings：`HOST`(預設 `127.0.0.1`)、`PORT`(預設 `50100`)、`POLICY_DIR`、`LIMIT_USAGE_ROUTING_URL`、`SIGNAL_TTL_SECONDS`、`RECOMMEND_TTL_SECONDS`。
  - 所有錯誤回應只給通用訊息，不得洩漏上游 URL、例外 traceback、檔案路徑（報告 §22）。
  - Log 每筆 recommend：時間、tier、role、recommended model、policy_version、狀態碼、耗時。**不得**記錄 request body 其餘內容。
  - `docker-compose.yml` 新增 `routing-policy` service：build `./routing-policy`、`ports: ["127.0.0.1:50100:50100"]`（**只有 localhost，不加 tailnet IP**，報告 §36）、`LIMIT_USAGE_ROUTING_URL` 指向 `http://limit-usage:50048/api/routing`、healthcheck 打 `/v1/health`、`restart: unless-stopped`。現有 `limit-usage` service 一個字都不要改。
  - `README.md`：職責邊界（報告 §63 三句話）、endpoint 說明、設定變數表、「本服務不持有任何 provider credential」聲明、如何本機驗證。
- **驗證**: `cd routing-policy && python -m pytest tests/ -v`（用 `fastapi.testclient.TestClient`，signals 以 stub 注入，不打真的 limit-usage）；另外 `docker compose config -q` 在 repo 根目錄必須成功。
- **依賴**: Task 2、Task 3
- **評分**: A=2 B=1 C=2 D=1 → 總分 6
- **Tier**: T2
- **指派**: 依 `/api/routing` 即時額度
- **審查**: 交叉審查（不同廠商）

### Task 5: Subagent Skill 改接 Routing Policy
- **檔案**: `~/.agents/skills/orchestrating-development/scripts/lib/routing.sh`、`~/.agents/skills/orchestrating-development/scripts/selftest`、`~/.agents/skills/orchestrating-development/references/routing.md`
- **要做什麼**:
  - `lib/routing.sh` 新增第 0 層：`route_policy TIER [AVOID_VENDOR]`，`POST $ROUTING_POLICY_URL/v1/recommend`，body 由 tier/role/avoid vendor/`DISPATCH_ROUTING_VENDORS`/`DISPATCH_ROUTING_MIN_SCORE` 組出。沿用既有的 `DISPATCH_ROUTING_HEADERS` 與 `CF_ACCESS_CLIENT_ID/SECRET` 機制（已存在，不要重寫）。
  - 三級 fallback（報告 §28–§30）：live → `~/.cache/subagent-routing/policy.json` 快取（以 response 的 `expires_at` 判新鮮；過期仍可用但印出 `stale policy cache` 到 stderr）→ 現有 `route_quota`（limit-usage `/api/routing`）→ 現有靜態階梯 `route_implement`。**現有兩層一行都不要刪**。
  - 新環境變數 `ROUTING_POLICY_SOURCE`：`central`（走新 API）｜`legacy`（完全跳過新 API，維持今天行為）。**預設必須是 `legacy`**——新服務尚未在外網部署，預設改行為會影響每一次派工。
  - 快取檔寫入用先寫 tmp 再 `mv` 的原子替換；權限 600。
  - `selftest` 新增案例：stub 一個 `/v1/recommend` server，驗證 (a) `central` 模式取得推薦、(b) server 掛掉時用快取、(c) 快取過期且 server 掛掉時退回靜態階梯、(d) 預設 `legacy` 模式下完全不呼叫新 API。整份 `selftest` 必須全綠。
  - `references/routing.md` 增一節說明三級 fallback 與新環境變數。
- **驗證**: `~/.agents/skills/orchestrating-development/scripts/selftest`（零 token，必須全綠）；另外 `bash -n scripts/lib/routing.sh`。
- **依賴**: Task 4
- **評分**: A=2 B=2 C=2 D=1 → 總分 7
- **Tier**: T2
- **指派**: 依 `/api/routing` 即時額度
- **審查**: 交叉審查（不同廠商）

## 未納入本計畫、需使用者決定

- **PR3 外網曝光**（D=3）：需要使用者指定主機名（例如 `routing.piea.uk`）並授權 `cf-tunnel-routes` 建立 route、Access application 與 per-host service token。
- **PR5–PR9**：等 OmniRoute 實際部署後才有 parity 對象。
