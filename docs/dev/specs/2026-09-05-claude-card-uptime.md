# Claude 卡片不掉線：本地官方額度來源 + OAuth 降為最後手段

日期：2026-09-05　分支：`task/claude-card-uptime`

## 背景（研究結論）

- 官方額度數字只在互動式 Claude Code TUI 有活動時，經 `claude-monitor --statusline` hook 寫進 `~/.claude-monitor/statusline/latest.json`。本機大多數 session 是 headless ACP，不會觸發 hook。
- claude-monitor 的 `--write-state` 在 capture 超過 600 秒後把視窗降成 `local_estimate`，provider 因此退回 OAuth 端點，端點被 429（Retry-After 3600）。過去 24 小時來源切換 7 次。
- Claude Code 自己把 usage 端點回應快取在 `~/.claude.json` 的 `cachedUsageUtilization`（含 `fetchedAtMs` 與完整 `limits[]`，含 Fable `weekly_scoped`），最多每 5 分鐘重抓，但只有 TUI 啟動或開對話框時才抓。
- `~/.claude.json` 不能單檔 bind-mount（inode 陷阱，同 `.credentials.json`）。

## 目標

1. 卡片在 TUI 閒置期間持續顯示最後一次官方數字，附資料時間，不再每 10 分鐘退回 OAuth。
2. Fable 每週視窗改由本地來源提供，取消 OAuth 補充呼叫。
3. OAuth 端點只在本地完全沒有可用數字時才打，間隔 30 分鐘，429 時完整遵守 Retry-After。

## 非目標

- 不修改 claude-monitor 套件本身（TTL 是上游設計）。
- 不從 ACP adapter 擷取 `rate_limit_event`。
- 不改前端 app.js；卡片的 `message` 與 `source` 既有渲染足夠。

## 資料來源與格式

### A. statusline capture（最新優先）

路徑設定 `claude_statusline_capture_path`，預設 `~/.claude-monitor/statusline/latest.json`。由 `claude-monitor --statusline` 以 tmp + rename 原子寫入。內容：

```json
{"captured_at_epoch": 1788533380,
 "rate_limits": {
   "five_hour":  {"used_percentage": 12.0, "resets_at": 1788534600},
   "seven_day":  {"used_percentage": 13.0, "resets_at": 1788591600},
   "spend_limit": {"used_percentage": 62.8, "resets_at": 1790787200},
   "model_scoped": [ {"title": "Current week (Fable)", "displayName": "Fable",
                      "limit": {"utilization": 2, "resets_at": 1788591600}} ]
 }}
```

- `rate_limits` 可能是 `null`（tombstone，免費方案或舊版），視為沒有視窗。
- `model_scoped` 是 Claude Code 2.1.261 schema 才有的欄位，可能不存在；`limit.resets_at` 可能是 epoch 整數或 ISO 字串或 null。
- 對應：`five_hour → key "5h"`, `seven_day → "1w"`, `spend_limit → "spend"`（label「Claude · 支出上限」，`limit_window_seconds=None`），`model_scoped[i] → "1w-<slug(displayName)>"`，label「Claude · 週額度 (<displayName>)」。
- 防 claude-monitor 已知的 leak bug：`used_percentage` 不是有限數字、< 0、或 > 101 → 該視窗略過；100 < x ≤ 101 → 夾到 100。
- 時間戳 = `captured_at_epoch`（缺或非數字 → 整個來源不可用）。

### B. Claude Code usage cache

路徑設定 `claude_usage_cache_path`，預設 `~/.claude-monitor/state/claude-code-usage.json`。由 host 端 timer 執行 `deploy/claude-usage-cache-sync.py` 從 `~/.claude.json` 抽出 `cachedUsageUtilization` 物件原樣寫入：

```json
{"fetchedAtMs": 1788575456000, "accountUuid": "…",
 "utilization": { "five_hour": {...}, "seven_day": {...}, "limits": [ {"kind":"session","percent":2,"resets_at":"…"}, {"kind":"weekly_all",…}, {"kind":"weekly_scoped","percent":2,"scope":{"model":{"display_name":"Fable"}},…} ] }}
```

- `utilization` 的形狀與 OAuth usage 端點回應相同，直接用既有 `parse_claude_usage(utilization)` 解析。
- 時間戳 = `fetchedAtMs / 1000`（缺或非數字 → 來源不可用）。

### C. OAuth usage 端點（最後手段）

既有邏輯保留：`GET https://api.anthropic.com/api/oauth/usage`，`parse_claude_usage(payload)`。

## 新鮮度與合併規則

- 設定 `claude_official_max_age_seconds`，預設 `21600`（6 小時，`ge=60`）。來源時間戳比 now 早超過此值、或檔案缺失／壞掉／無時間戳 → 該來源忽略，並記下原因字串。
- A、B 兩來源依時間戳由新到舊排序；逐來源加入視窗，key 已存在者不覆蓋（每個 key 取最新有它的來源）。
- 每個視窗 `raw_extra["observed_at"]` = 該來源時間戳 ISO 字串；`raw_extra["source"]` = `"statusline"` 或 `"claude-code-cache"`。
- **視窗重置（rollover）**：合併後若視窗有 `resets_at` 且 `now >= resets_at`：
  - key `"5h"`：`used_percent=0.0, remaining_percent=100.0, resets_at=None`，`raw_extra["rolled_over"]=True`。
  - key `"1w"` 或 `"1w-*"`：`used_percent=0.0, remaining_percent=100.0`，`resets_at` 以 7 天為步長推進到大於 now，`raw_extra["rolled_over"]=True`。
  - 其他 key（如 `spend`）：丟棄該視窗。
- 本地結果「可用」的定義：合併後含 key `"5h"` 或 `"1w"` 至少其一。
- 可用時：`status=OK`，`source` 為實際貢獻視窗的來源名稱，statusline 在前，以 `+` 連接（`"statusline"`, `"claude-code-cache"`, `"statusline+claude-code-cache"`）；`fetched_at=now`；`min_interval_seconds=60`。
  - `message`：貢獻來源中最新時間戳的年齡 ≤ 900 秒 → `None`；否則 `官方額度資料為 {age} 前`，age < 3600 秒用 `f"{age // 60} 分鐘"`，否則 `f"{age / 3600:.1f} 小時"`。
- 不可用時 → OAuth fallback：`min_interval_seconds = claude_oauth_min_interval_seconds`（預設 `1800`，`ge=60`）。錯誤訊息末尾附 `(local sources: <A 原因>; <B 原因>)`。OAuth 成功時視窗照舊，`source="oauth/usage"`，不做 rollover（端點回的是即時值）。

## 移除

- `parse_monitor_state`、`MONITOR_*` 常數、`_read_monitor_state`、claude-monitor state 檔來源。
- `_scoped_supplement`、`_store_scoped`、`parse_scoped_usage`、`SCOPED_*` 常數。
- 設定 `claude_monitor_state_path`、`claude_monitor_max_age_seconds`、`claude_scoped_refresh_seconds`、`claude_scoped_max_age_seconds` 與對應 `claude_monitor_state` property。
- 對應測試。

## Poller

`app/services/poller.py::_schedule_next_fetch`：目前 `delay = min(delay, max_backoff_seconds)` 會把 Retry-After 3600 截成 900。改為在套用上限之後再 `delay = max(delay, retry_after)`，讓伺服器指定的等待時間永遠被完整遵守；其餘退避邏輯不變。

## 部署

- `deploy/claude-usage-cache-sync.py`（stdlib only，python3）：argparse `--config`（預設 `~/.claude.json`）、`--out`（預設 `~/.claude-monitor/state/claude-code-usage.json`）。讀取 config JSON 的 `cachedUsageUtilization`；缺失或不是物件 → 不寫檔、exit 0。若 out 已存在且 `fetchedAtMs` 相同 → 不寫。否則 mkdir -p 父目錄，tmp + `os.replace` 原子寫入，exit 0。任何例外 → stderr 一行訊息，exit 1。
- `deploy/limit-usage-claude-monitor.service`：`ExecStart=/usr/bin/python3 %h/Container/limit-usage/deploy/claude-usage-cache-sync.py`，Description 改為 `Sync Claude Code usage cache for the limit-usage Claude card`，移除 claude-monitor 相關註解與 Documentation 行。單元名稱維持不變（已安裝、已 enable）。
- `deploy/limit-usage-claude-monitor.timer`：週期維持 2 分鐘，註解改成引用 `CLAUDE_OFFICIAL_MAX_AGE_SECONDS`。
- `docker-compose.yml`：`${HOME}/.claude-monitor/state:/secrets/claude-monitor:ro` 改為 `${HOME}/.claude-monitor:/secrets/claude-monitor:ro`；environment 移除 `CLAUDE_MONITOR_STATE_PATH`，新增 `CLAUDE_STATUSLINE_CAPTURE_PATH: "/secrets/claude-monitor/statusline/latest.json"` 與 `CLAUDE_USAGE_CACHE_PATH: "/secrets/claude-monitor/state/claude-code-usage.json"`。
- `.env.example` 與 README 設定表同步：新增 `CLAUDE_STATUSLINE_CAPTURE_PATH`、`CLAUDE_USAGE_CACHE_PATH`、`CLAUDE_OFFICIAL_MAX_AGE_SECONDS`（21600）、`CLAUDE_OAUTH_MIN_INTERVAL_SECONDS`（1800）；移除 `CLAUDE_MONITOR_STATE_PATH`、`CLAUDE_MONITOR_MAX_AGE_SECONDS`、`CLAUDE_SCOPED_REFRESH_SECONDS`、`CLAUDE_SCOPED_MAX_AGE_SECONDS`。
- README「Claude quota via claude-monitor」章節改寫為三來源說明（A/B/C、新鮮度、rollover、`source` 值），setup 改為 hook + 本 repo 的 sync timer，保留 bind-mount gotcha。

## 測試策略

- 純函式：capture 解析（正常、tombstone、leak bug、model_scoped 缺失／epoch／ISO）、cache 解析、合併（key 取最新來源）、rollover 三種 key、年齡訊息格式。
- provider `fetch()`：只有 capture、只有 cache、兩者合併、兩者過期 → OAuth、OAuth 429 訊息含 local 原因、`min_interval_seconds` 在兩種路徑的值。
- poller：retry_after 3600 且 max_backoff 900 → 下次抓取 ≥ now+3600。
- sync script：抽取、原子寫入、fetchedAtMs 相同不重寫、缺 key 不寫檔。

## 假設

- 使用者已核准研究報告中的三點做法（「開始實作」）；此規格記錄設計，未另行等待核准。
- weekly 視窗以固定 7 天週期重置，rollover 推進 7 天是準確的；5h 視窗重置後起點不可知，故 `resets_at=None`。

## 修訂（2026-09-05，全分支審查後）

全分支審查指出：OAuth fallback 成功後 provider 把 `min_interval_seconds` 切成 1800，於是本地檔案在之後 30 分鐘內即使變新也不會被重讀；配合 poller 完整遵守 Retry-After，一次 429 更會讓本地來源被忽略一小時。這與目標 1 相悖。修訂如下：

- `min_interval_seconds` 固定為 60，不再依路徑切換。poller 每分鐘呼叫 `fetch()`，本地來源永遠先讀。
- OAuth 節流改由 provider 內部負責：`_oauth_next_attempt_at` 在每次呼叫前設為 `now + claude_oauth_min_interval_seconds`；429 時設為 `now + max(Retry-After, claude_oauth_min_interval_seconds)`。provider 不再設定 `retry_after_seconds`（恆為 None）。
- 本地不可用且尚未到 `_oauth_next_attempt_at` 時：若有上一次成功的 OAuth 快照（`_last_oauth_snapshot`）就回傳它，`status=OK`、`source="oauth/usage"`、`fetched_at` 維持原值、`message = f"沿用 {format_age(age)}前的 OAuth 額度（本地來源：{local_note}）"`；否則回傳 `RATE_LIMITED` 的 error snapshot，message `f"OAuth fallback rationed; next attempt in {wait}s (local sources: {local_note})"`，交給 poller 沿用 DB 裡的上次額度。
- `message` 文案定案為 `官方額度資料為 {format_age(age)}前`（無空格），以程式碼為準。
- Task 2 的 poller 修改保留：任何 provider 若設定 `retry_after_seconds` 仍會被完整遵守，只是 Claude provider 不再使用它。
