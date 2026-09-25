# 計畫：模型 Tier 由成本與 benchmark 計算

規格：`docs/dev/specs/2026-09-26-model-tier-derivation.md`

兩個工區：
- **A** = limit-usage，`~/Container/limit-usage-model-tier-derivation`（branch `feat/model-tier-derivation`，base `origin/main` 8bd9498）
- **B** = skill，`~/Container/skill-orchestrating-development-model-tier`（branch `feat/model-tier-derivation`，base 本機 `main` 44355ba）

派工波次：

```
波 1（並行）: Task 1 (A) ─┐        Task 3 (A)        Task 7 (B) → Task 6 (B)
波 2:          Task 2 (A) ←┘
波 3（並行）: Task 4 (A) ← Task 1, Task 2      Task 5 (A) ← Task 2
波 4:          Task 8 (A) ← 全部 A 任務
```

各 task 的「指派」「審查」寫 `quota`：派工當下由 `dispatch` 依即時額度路由決定，不預先指定。

### Task 1: 建立 `catalog/` 套件（模型資料 + Tier 公式）
- **工區**: A
- **檔案**: `catalog/__init__.py`、`catalog/models.yaml`、`catalog/tiering.yaml`、`catalog/tiering.py`、`tests/test_catalog.py`
- **要做什麼**: 依規格〈catalog 契約〉建立三個檔案與公開介面 `load_catalog`、`tier_candidates`、`Derived`、`Catalog`。`models.yaml` 的 pools 原樣搬自 `routing-policy/policy/models.yaml`；16 個模型的 bench、pool、slots、dispatchable 沿用現值，價格、effort 依規格 golden table，`bench_note` 依 brief 附的原文。只用 stdlib + PyYAML。`tests/test_catalog.py` 逐欄比對 golden table，並為每條驗證規則各寫一個失敗案例（未知 key、bench 超出 0–100、負價格、未知 pool、未知 slot、`proxy_of` 指向不存在的模型、門檻非遞增、T0 min_bench ≠ 0）。
- **驗證**: `.venv/bin/python -m pytest tests/test_catalog.py -q` 全數通過
- **依賴**: 無
- **評分**: A=2 B=1 C=1 D=0 → 4
- **Tier**: T1
- **指派**: quota
- **審查**: 編排者讀 diff

### Task 2: limit-usage `/api/routing` 改由 catalog 推導
- **工區**: A
- **檔案**: `app/services/routing_view.py`、`app/main.py`、`Dockerfile`、`requirements.txt`、`tests/test_routing_view.py`、`tests/test_routing_feedback.py`
- **要做什麼**: 刪除 `MODELS`、`TIER_LEVELS` 常數與 `tier_candidates`，改由 `catalog.tiering.load_catalog()` 在模組載入時讀取（`POOLS` 改取自 catalog）；`models.<id>`、`tiers.<T>.candidates[]`、`orchestrators` 依規格〈API 變更〉增刪欄位（`listed`、`missing` 在本 task 固定輸出 `null`，由 Task 5 接上）；新增頂層 `catalog` 區塊（`uncatalogued: {}`、`cli_models: null` 先留空）；排序最後一鍵 `cost_rank` → `blended_price`；`app/main.py` 的 feedback 端點改用 catalog 解析 model → pool。`Dockerfile` 加 `COPY catalog ./catalog` 與 `RUN pip install --no-cache-dir 'PyYAML>=6'`；`requirements.txt` 加 `PyYAML>=6`。兩個測試檔改用 `tmp_path` 內的虛構 catalog（`CATALOG_DIR` 或直接傳目錄），斷言不引用真實模型 id；另留一個真實 catalog 煙霧測試。
- **驗證**: `.venv/bin/python -m pytest tests -q` 全數通過；`grep -nE "cost_rank|MODELS\b" app/services/routing_view.py app/main.py` 無結果
- **依賴**: Task 1
- **評分**: A=2 B=2 C=2 D=1 → 7
- **Tier**: T2
- **指派**: quota
- **審查**: quota

### Task 3: 主機端 CLI 模型清單匯出
- **工區**: A
- **檔案**: `deploy/cli-models-export.py`、`deploy/limit-usage-cli-models.service`、`deploy/limit-usage-cli-models.timer`、`tests/test_cli_models_export.py`
- **要做什麼**: 依規格〈匯出程式〉實作，只用 stdlib；解析函式與執行分開，讓測試用 fixture 字串（brief 附三家實際輸出樣本）。systemd 單元比照 `deploy/limit-usage-claude-monitor.*`。
- **驗證**: `.venv/bin/python -m pytest tests/test_cli_models_export.py -q` 通過；`python3 deploy/cli-models-export.py /tmp/cli-models-test.json && python3 -m json.tool /tmp/cli-models-test.json` 成功且 codex、agy、grok 皆 `ok: true`
- **依賴**: 無
- **評分**: A=2 B=1 C=1 D=1 → 5
- **Tier**: T1
- **指派**: quota
- **審查**: 編排者讀 diff

### Task 4: routing-policy 改讀 catalog
- **工區**: A
- **檔案**: `routing-policy/app/policy.py`、`routing-policy/app/engine.py`、`routing-policy/app/signals.py`、`routing-policy/app/main.py`、`routing-policy/policy/models.yaml`（刪除）、`routing-policy/policy/roles.yaml`、`routing-policy/policy/tiers.yaml`、`routing-policy/Dockerfile`、`docker-compose.yml`、`routing-policy/tests/*`
- **要做什麼**: 依規格〈API 變更 → routing-policy〉。`Policy` 的模型、candidates、reviewer、orchestrator 全部來自 `catalog.tiering`；`orchestrator.excluded_vendors` 改讀 `tiering.yaml`；`signals` 加讀 `models.<id>.usable`，engine 排除 `usable == false`；`/v1/recommend` 回應的 `recommended`／`alternatives[]` 加 `effort`；`policy_version` → `"2026-09-26.1"`；compose build 改 `{context: ., dockerfile: routing-policy/Dockerfile}`，Dockerfile 調整 COPY 並加 `COPY catalog ./catalog`；parity 測試改為兩邊讀同一份 catalog。
- **驗證**: `cd routing-policy && ../.venv/bin/python -m pytest -q` 全數通過；`docker compose config` 成功；`docker build -f routing-policy/Dockerfile .` 成功
- **依賴**: Task 1、Task 2
- **評分**: A=3 B=2 C=2 D=1 → 8（A=3 強制升 T3）
- **Tier**: T3
- **指派**: quota
- **審查**: quota

### Task 5: 下架偵測接進 limit-usage
- **工區**: A
- **檔案**: `app/services/cli_models.py`（新）、`app/config.py`、`app/services/routing_feedback.py`、`app/services/routing_view.py`、`app/main.py`、`tests/test_cli_models.py`（新）、`tests/test_routing_feedback.py`
- **要做什麼**: 依規格〈limit-usage 讀取〉與〈派工失敗回報〉：讀 `data/cli-models.json`（mtime 快取、48 小時新鮮度），填 `models.<id>.listed`、`catalog.uncatalogued`、`catalog.cli_models`；`listed == false` → `usable = false`；feedback `status == 404` 帶 model → 單一模型下架 24 小時，不動 pool；成功回報清除；`GET /api/routing/feedback` 加 `models` 區塊；`models.<id>.missing` 輸出標記 view。
- **驗證**: `.venv/bin/python -m pytest tests -q` 全數通過
- **依賴**: Task 2
- **評分**: A=2 B=2 C=2 D=0 → 6
- **Tier**: T2
- **指派**: quota
- **審查**: quota

### Task 6: skill 路由去除模型名稱
- **工區**: B
- **檔案**: `scripts/lib/routing.sh`、`scripts/dispatch`、`scripts/route`、`scripts/selftest`
- **要做什麼**: 依規格〈skill 變更〉的 scripts 部分全部項目，含 selftest 的虛構 stub、新案例與「skill 無具體模型名稱」檢查。
- **驗證**: `scripts/selftest` 全數通過，且輸出含模型名稱檢查那一條為 ok
- **依賴**: Task 7
- **評分**: A=2 B=2 C=2 D=0 → 6
- **Tier**: T2
- **指派**: quota
- **審查**: quota

### Task 7: skill 文件去除模型名稱
- **工區**: B
- **檔案**: `references/routing.md`、`SKILL.md`
- **要做什麼**: 依規格〈skill 變更〉的文件部分：刪除所有具體模型名稱與依模型的表格，改寫成 catalog 位置、公式、`route --list`、兜底順序（快取 → `DISPATCH_ROUTING_FALLBACK_VENDORS` 廠商預設）。CLI 旗標對照、相容性矩陣、額度回報等不含模型名稱的段落保留。
- **驗證**: `grep -rEn '\b(claude|gpt|grok|gemini)-[a-z]*-?[0-9]|\b(Opus|Sonnet|Haiku|Fable|Mythos|GPT-?|Grok|Gemini) ?[0-9]' SKILL.md references` 無結果
- **依賴**: 無
- **評分**: A=1 B=2 C=0 D=0 → 3
- **Tier**: T1
- **指派**: quota
- **審查**: 編排者讀 diff

### Task 8: limit-usage 文件同步
- **工區**: A
- **檔案**: `README.md`、`routing-policy/README.md`、`docs/dev/specs/2026-09-15-routing-policy-architecture.md`
- **要做什麼**: README 的 routing 欄位表、cooldown 規則（補 404 單一模型下架）、`MODELS` 說明改為 catalog 與公式、新增 cli-models timer 安裝方式、測試數量；routing-policy README 的範例與排序說明；2026-09-15 架構規格只在開頭加一行「模型資料已移到 `catalog/`，見 2026-09-26 規格」。
- **驗證**: `grep -nE "cost_rank|max_tier.*人工|routing_view.py.*MODELS" README.md routing-policy/README.md` 無結果
- **依賴**: Task 2、Task 3、Task 4、Task 5
- **評分**: A=1 B=1 C=0 D=0 → 2
- **Tier**: T0
- **指派**: quota
- **審查**: 編排者讀 diff

## 矛盾掃描

- Task 2 與 Task 5 都改 `routing_view.py`、`main.py`、`tests/test_routing_feedback.py` → 串行（5 在 2 之後）。
- Task 4 與 Task 5 檔案不重疊 → 可並行。
- Task 6 的模型名稱檢查會掃 `references/`，所以 Task 7 先做。
- 與 Global Constraints 無衝突。
