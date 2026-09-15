# 規格 — Routing Policy Service（節錄自使用者架構報告 2026-09-15）

本檔是使用者提供之《limit-usage 瘦身、OmniRoute 整合與跨網路 Subagent Routing 架構規劃報告》
的實作面節錄，作為本次 PR1–PR4 的規格來源。三層責任劃分與 API 形狀直接引用報告。

## 目標

把目前混在 `limit-usage` 裡的「Agent 決策政策」抽成獨立的 `routing-policy` 服務，讓沒有加入
Tailscale 的主機也能透過一個**狹窄、唯讀、不含任何 provider credential** 的 API 取得同一套
Subagent 選模政策。

## 非目標

- 不建 AI Gateway、不做 inference、不持有 provider OAuth／API key、不管理帳號。
- 本階段不刪除 `limit-usage` 的 `/api/routing`、`routing_view.py`、`routing_feedback.py`。
- 本階段不接 OmniRoute（本機未部署）。

## 三層責任（報告 §2、§5、§63）

| 元件 | 回答的問題 | 負責 |
|---|---|---|
| OmniRoute | How should the AI request actually run? | provider/account 選擇、health、fallback、circuit breaker、實際 inference |
| routing-policy | Which class of model should this agent task use? | T0–T3、implement/review/orchestrate 角色、reviewer 政策、cross-vendor、候選排序、logical route |
| limit-usage | What is actually happening outside the AI gateway? | SuperGrok 權威 quota、Claude local verifier、UPS、backup、system health、個人資料 |

不要三層都做同一件事（報告 §31）：Skill 只決定 tier/role/constraints；policy 只決定能力等級與候選排序；
OmniRoute 只決定實際執行與失敗 fallback。

## Policy 是設定，不是程式碼（報告 §12）

模型能力（`max_tier`、`bench`、`cost_rank`、`role`、`reviewer`、`dispatchable`）、tier 區間、
review 的 `cross_vendor` 規則、ranking 順序，全部寫進版本控制的 YAML。
排序優先序：`availability → quota → capability → cost`。

## API（報告 §18–§21、§56）

versioned `/v1`，三個 endpoint：

- `GET /v1/health` → `{"status":"ok","policy_version":"..."}`。**只有這兩個欄位**，不得塞 provider
  credential health、quota、內部 error dump。
- `GET /v1/policy` → `{schema_version, policy_version, generated_at, tiers:{T0..T3:{complexity:[lo,hi]}}, review:{cross_vendor}}`。
  描述「現在使用的是哪套方法」，不含模型明細。
- `POST /v1/recommend`，body `{tier, role, implemented_by_vendor?, client:{available_vendors?}?, min_score?}`
  → `{policy_version, generated_at, expires_at, tier, role, recommended:{vendor,model}|null, alternatives:[...], reason_codes:[...], wait_seconds}`。
  reviewer 不另開 endpoint：`role="review"` + `implemented_by_vendor` + policy `cross_vendor=true`
  即自動排除該 vendor。

## 絕不回傳的資訊（報告 §22、§23）

OAuth access/refresh token、API key、帳號 email／ID、cookie、credential 路徑、完整 upstream error、
內部 host name、SQLite 路徑、完整 provider quota payload、OmniRoute admin 資訊。

**預設不回傳真實 quota 百分比**。需要表達額度時只給 `healthy|degraded|unknown`。
排序仍由 Policy Service 內部用真實分數完成，但那不出現在 response 裡。

## Reason codes（報告 §41）

推薦要可解釋，但不得輸出完整內部演算法。固定字串集合，例如 `tier_capable`、`quota_healthy`、
`provider_healthy`、`cross_vendor_review`。

## Logging（報告 §42）

記：timestamp、client machine、tier、role、recommended model、policy_version、狀態碼、latency。
**不記**：完整 prompt、完整 task 內容、API key、OAuth、provider token。Policy API 不需要知道工作全文。

## 安全邊界（報告 §36、§37、§39）

- origin 只 listen `127.0.0.1`（例如 `127.0.0.1:50100`），不用 `0.0.0.0`；對外由 `cloudflared`
  主動建立 outbound tunnel。
- 外網驗證用 Cloudflare Access Service Auth（`CF-Access-Client-Id` / `CF-Access-Client-Secret`），
  每台主機一把 token，可獨立 revoke。Secret 不寫進 Skill，由環境變數注入。
- **不要因為有 Cloudflare Access 就直接暴露整個 limit-usage**。新的 Internet surface 必須是
  一個完全獨立的小服務。
- 三種 credential class 必須分離：Class A routing read／Class B AI inference／Class C admin。

## Client 的三級 fallback（報告 §28–§30、§55）

```
central live policy → last-known-good cache（帶 expires_at 判新鮮）→ built-in 靜態階梯
```

中央 Policy API 不得成為整個 agent system 的 single point of failure。
遷移期保留 `ROUTING_POLICY_SOURCE=legacy|central` 兩條路。

## Policy versioning（報告 §40）

每次 policy 變更都有 `policy_version`（例如 `2026-09-15.1`），每次 recommendation 都回傳，
以便事後回答「這個 Subagent 當時是按哪一版 policy 派出去的」。

## 遷移階段（報告 §46–§54）

Phase 1 資料流（本次實作）：

```
Skill → tier → routing-policy → 現有 routing_view 邏輯的抽出版 → 推薦 → 遠端主機跑本地 CLI
```

第一版**不要改演算法**：先完成 responsibility extraction，再談改算法。
不要「搬家 + 重寫」同時進行。
