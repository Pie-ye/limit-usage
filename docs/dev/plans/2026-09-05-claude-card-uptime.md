# 計畫：Claude 卡片不掉線

規格：`docs/dev/specs/2026-09-05-claude-card-uptime.md`。三個 task 檔案互不重疊，並行派工。

### Task 1: ClaudeProvider 改讀 statusline capture + Claude Code usage cache，OAuth 降為最後手段
- **檔案**: `app/providers/claude.py`、`app/config.py`、`app/providers/registry.py`、`tests/test_claude_parse.py`
- **要做什麼**: 見 task-1-brief（規格「資料來源與格式」「新鮮度與合併規則」「移除」）
- **驗證**: `/home/pieye/Container/limit-usage/.venv/bin/python -m pytest -q` 全綠
- **依賴**: 無
- **評分**: A=2 B=2 C=2 D=0 → 總分 6
- **Tier**: T2
- **指派**: codex gpt-5.6-luna
- **審查**: claude claude-opus-5

### Task 2: Poller 完整遵守 Retry-After
- **檔案**: `app/services/poller.py`、`tests/test_poller.py`
- **要做什麼**: `_schedule_next_fetch` 套用 `max_backoff_seconds` 上限之後再 `max(delay, retry_after)`；新增測試
- **驗證**: `/home/pieye/Container/limit-usage/.venv/bin/python -m pytest -q tests/test_poller.py`
- **依賴**: 無
- **評分**: A=1 B=1 C=1 D=0 → 總分 3
- **Tier**: T1
- **指派**: claude claude-sonnet-5
- **審查**: codex gpt-5.6-luna

### Task 3: 部署與文件：usage cache sync 腳本、systemd 單元、compose、.env.example、README
- **檔案**: `deploy/claude-usage-cache-sync.py`（新）、`deploy/limit-usage-claude-monitor.service`、`deploy/limit-usage-claude-monitor.timer`、`docker-compose.yml`、`.env.example`、`README.md`、`tests/test_claude_usage_cache_sync.py`（新）
- **要做什麼**: 見 task-3-brief（規格「部署」）
- **驗證**: `/home/pieye/Container/limit-usage/.venv/bin/python -m pytest -q tests/test_claude_usage_cache_sync.py`；`docker compose config` 解析成功
- **依賴**: 無（設定名稱由規格固定）
- **評分**: A=2 B=1 C=1 D=1 → 總分 5
- **Tier**: T1
- **指派**: claude claude-sonnet-5
- **審查**: codex gpt-5.6-luna
