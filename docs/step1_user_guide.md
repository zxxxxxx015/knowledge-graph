# Step 1 使用手冊：知識條文拆分

## 目的與範圍

Step 1 將 **Step 0 JSON 的 `processed_text`** 拆成具有完整語意的知識條文，只決定拆分、合併邊界。程式不重新開啟來源 Markdown，不改寫內容、不解決指涉、不提取知識項、不做六大知識包分類，也不建立因果圖譜。本輪沒有執行真實 LLM。

目前實際工作區是 `C:\Users\User\OneDrive\Desktop\知識圖譜`。下方相對路徑命令均從此專案根目錄執行；不使用施工指令中另一個未含 OneDrive 的路徑。

## 檔案與公開介面

- [Pipeline](../src/pipeline/step1_split.py)：`load_step0`、`Step1Splitter.process`、`validate_result`、`save_result`、`run_step1`。
- [唯一正式 Prompt](../prompts/step1.md)。沒有多版本切換。
- [LLM Schema](../schemas/step1_llm.schema.json)：只接收邊界、原文核對文字及語意依據。
- [Result Schema](../schemas/step1_result.schema.json)：完整正式輸出。
- [獨立 CLI](../scripts/run_step1.py)。匯入模組不啟動 LLM。
- [離線測試](../tests/test_step1.py)。使用 unittest 與 Fake Client，封鎖真實子程序啟動。

沿用既有 [CodexClient](../src/llm/codex_client.py)、[Step 0 驗證器](../src/pipeline/step0_preprocess.py) 與 [Step 0 Schema](../schemas/step0_result.schema.json)，沒有修改它們。Result Schema 內嵌本次 Step 0 欄位定義，因此無須遠端解析 Schema；執行時仍使用既有 Step 0 驗證器核對。

## 正式輸入

只接受 UTF-8 `.json`，內容必須是新版 Step 0 正式結果。拒絕 `.md`、`.txt`、重複 JSON 欄位、NaN、無效 UTF-8、錯誤 Schema、無法重建的 `processed_text`、錯誤來源 SHA-256 及 `llm_failed`。

新版欄位為 `source.document_type`、`source.size_bytes`、`source.char_count`、`status.execution_state`。不轉換舊版欄位，也不修改 Step 0。`completed_with_warnings` 可正常輸入，但 Step 0 必須通過完整性檢查；它的 unresolved、warnings 與安全拒絕紀錄均保留。

目前正式輸入：[最新 Sample Step 0 JSON](../outputs/step0/20261029-134323/sample_technical_incident.step0.json)。來源文字已完整內嵌其中，因此即使原始 Markdown 移動，拆分仍可進行。

## 一行 PowerShell 正式執行指令

以下指令會呼叫真實 LLM，留待使用者手動執行。時間戳記建立新的歷史目錄；若同名結果已存在，程式拒絕覆寫。

```powershell
.\.venv\Scripts\python.exe .\scripts\run_step1.py --input .\outputs\step0\20261029-134323\sample_technical_incident.step0.json --output-dir (Join-Path .\outputs\step1 (Get-Date -Format 'yyyyMMdd-HHmmss-fffffff')) --timeout 180 --max-retries 0
```

產生的檔案名稱是 `sample_technical_incident.step1.json`。CLI 印出 `output_path` 與 `status`。一般輸入 `name.step0.json` 會輸出 `name.step1.json`；其他合法 Step 0 JSON 檔名則在 stem 後加 `.step1.json`。

輸出目錄自動建立；歷史檔案、來源文件與 Step 0 JSON 不會被覆寫。執行前會檢查輸出位置及寫入權限。寫入採同目錄暫存檔、flush/fsync 及 `os.link` 原子發布；競爭寫入時只有第一個可成功，不會取代既有檔案。發布失敗會清除暫存檔。

退出碼：`0` 為成功（也包括有明確警告的成功）；`1` 為 LLM 呼叫失敗；`2` 為參數、輸入、邊界、完整性或檔案錯誤；`130` 為使用者中斷。驗證失敗不發布新的正式 JSON；可能留下已建立的空輸出目錄。

## 實際資料流程

1. 讀取 Step 0 JSON 的原始 bytes，計算輸入檔 SHA-256、保留完整 JSON 資料快照。
2. 使用既有 Step 0 Schema 與驗證器，核對來源大小、來源 hash、替換位置、重建文字、unresolved、warning 及狀態統計。
3. 掃描 `processed_text` 的 Markdown。建立標題階層、可解析表格及受保護區域；`.txt` 來源保留普通文字語意，不套用 Markdown 結構規則。
4. 使用唯一 Prompt 呼叫 `CodexClient.generate_json`。只提供處理後文字、行範圍、機械邊界提示、結構與可拆分區間。原文映射不傳給模型，原文不作為替代拆分來源。
5. 驗證模型 Schema、逐字核對位置、檢查表格整列邊界及語意合併理由標記，禁止切斷 inline code、越界或重疊。不搜尋、不模糊比對、不自動修正 offset。
6. Python 按原始切片擷取條文；建立穩定 ID、原文位置映射、章節及 Step 0 索引追溯。
7. 所有非空白字元必須落在條文或有理由的未處理區域；漏記任何正文即拒絕整批。純空白間隙保存在完整 `processed_text`。
8. 驗證 Result Schema 與所有衍生 metadata。發布前重新讀取 Step 0 JSON，確認 bytes hash 與內容未在執行途中改變，再原子寫入。

空白文件、只有標題、只有受保護內容時可不呼叫 LLM，仍產生可驗證結果。

## 拆分原則

- 完整單句為基本單位；同句包含多個動作仍可是一條。
- 相互獨立且完整的句子分開；相同主題不足以支持合併。
- 前導句加清單若共同補完陳述，原樣合併；獨立完整的清單項目可各成一條。
- 兩句明確因果承接可合併；三句以上連續因果鏈也可合併，不設固定句數上限。每個相鄰關係都須有文字依據；保留原始各行，不轉成箭頭或摘要。
- 不以 29 條作通用目標。句子完整性與語意依賴交給 LLM，Python 驗證位置、資料保留及結構安全。
- 標題存為章節脈絡；以標題 level stack 計算祖先，上一個同級標題不會成為父章節。支援 ATX 與單行 Setext 標題。

## 表格、程式碼及其他 Markdown

簡單 pipe table 以能獨立形成陳述的資料列為預設單位。可以沒有外側管線符號；支援跳脫管線 `\|`。欄數必須一致、至少兩欄、欄名必須非空且不重複；表頭分隔列使用三個以上連字號及可選對齊冒號。

每條表格條文保留原始整列，**包含原有行尾換行**。相鄰多列不可分割時，LLM 必須以 `table_dependency` 提供明確說明；範圍必須正好涵蓋連續完整資料列。不允許跨表或與外部正文混成一條。

`table_context` 保留整表、整表原文映射、表頭、分隔列、所屬資料列及各儲存格。儲存格 `text` 包含原有前後空白；`column_index` 從 0 開始，`header_range` 與 `header_text` 指向原始表頭儲存格，不自行補欄名。

欄數錯誤、缺漏表頭或分隔列、空儲存格、疑似合併儲存格、多行儲存格、HTML table、HTML 儲存格、反引號表格內容等，會保守存為 `table_pending`，不猜測。無法可靠區分的 pipe 文字也可能保留為 pending。成功轉成條文的資料列不會再次成為未處理區域；表頭和分隔列會保存為 `table_structure`。

fenced code（含未閉合 fence）、縮排程式碼與 HTML pre/script/style 區塊存為 `code_block`。有 4 個以上前置空白的巢狀清單目前也可能保守當成 code。一般 inline code 可包含在完整自然語言條文內，但禁止在它內部切割。Front matter、其他 HTML 區塊、引用段落、連結定義等未完整支援的 Markdown 保留為 `unsupported_markdown`。這是保守掃描器，不是完整 CommonMark/GFM 渲染器。

## 輸出 JSON 欄位

| 頂層欄位 | 內容 |
| --- | --- |
| `input` | Step 0 JSON 絕對路徑、輸入原始 bytes 的 SHA-256、bytes 大小。 |
| `source` | Step 0 source 原樣保留，含來源文件的 SHA-256。 |
| `original_text` | 完整原文，包含 BOM、空白與所有原始換行。 |
| `processed_text` | Step 0 處理後全文，唯一拆分來源。 |
| `step0_provenance` | replacements、unresolved、warnings、status 原樣保存。 |
| `structure` | headings、tables、protected_regions。表格保存完整欄位對應。 |
| `knowledge_statements` | 條文陣列，依原始位置排序。 |
| `unhandled_regions` | 未成條文的文字、位置、原因及追溯。 |
| `warnings` | Step 1 明確警告；與 Step 0 原始 warnings 分開。 |
| `status` | completed / completed_with_warnings、Prompt ID/hash、LLM 嘗試與重試數、條文及未處理範圍數。 |

每條知識條文包含：

| 欄位 | 說明 |
| --- | --- |
| `statement_id` | `ks_` 加 64 位 SHA-256。 |
| `source_ref` | 來源文件 SHA-256，對應頂層 source。 |
| `text` | 嚴格等於 `processed_text[start:end]`。 |
| `kind` | prose、list 或 table。 |
| `boundary_reason` | complete_sentence、independent_sentence、lead_list_dependency、causal_dependency、causal_chain、table_row 或 table_dependency。 |
| `boundary_detail` | LLM 提供的拆分／合併語意依據，只作 metadata。 |
| `processed_range` | Python Unicode code point 半開字元範圍。不是 bytes、UTF-16 或行號。 |
| `original_mapping` | available / unavailable、source_range、原文 text、不能映射時的 reason。 |
| `heading_context` | 根章節至目前章節的祖先序列，含標題位置與 parent_id。 |
| `step0_trace` | replacements、unresolved、warnings 的 0-based 索引及無法定位紀錄。 |
| `table_context` | 僅 table 條文存在；整表與表頭／資料列／儲存格的精確映射。 |

## 穩定 ID

使用如下固定序列化方式，沒有隨機 UUID：

```python
payload = json.dumps(
    [source_sha256, start, end, statement_text],
    ensure_ascii=False, separators=(",", ":"),
)
statement_id = "ks_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

相同來源 bytes hash、相同邊界與文字，跨次執行可重現同一 ID。路徑、時間戳記、模型說明不參與雜湊；模型選擇不同邊界時 ID 自然不同。

## 追溯回 Step 0 與原文

1. `input.sha256` 是 **Step 0 JSON 檔案 bytes** 的 hash；`source.sha256` 是 **原始來源文件 bytes** 的 hash，兩者用途不同。
2. 使用條文 `processed_range` 切片完整 `processed_text`，結果必須與條文 `text` 完全一致。
3. `original_mapping` 按每次替換前後的長度差，將兩端邊界映射回 `original_text`，並保存完整原文切片。邊界剛好在替換兩端可精確映射；落在替換字串內部則標記 unavailable，source_range/text 為 null，不猜測。該條文的處理後切片及替換紀錄仍完整存在。
4. `step0_trace.replacement_indexes` 對應 `step0_provenance.replacements` 的原始索引；unresolved_indexes 與 warning_indexes 亦同。無排序改編原陣列。表格條文也關聯它使用的表頭／分隔列紀錄。
5. trace 依來源位置相交建立。`unlocated_unresolved_indexes`、`unlocated_warning_indexes` 保存 source_range=null 的全域未定位紀錄；它們不能被當作已確認的局部位置。`python_validation` origin 原樣保留。即使條文已形成，未解指涉仍是未解。
6. 表格、表頭、分隔列、列及儲存格都有 processed_range、text、original_mapping。未處理區域與標題也保存原始文字／位置，不會因未成條文而丟失。
7. `validate_result(result)` 可在沒有來源 Markdown 的環境驗證內部結構、切片、mapping、trace、ID 與覆蓋。若還要驗證輸入 JSON 檔案的實際 bytes hash，使用 `validate_result(result, load_step0(input_path))`。單憑輸出內的 hash 不能證明磁碟上的輸入仍相同；發布函式會實際讀取並比對。

## 離線測試

不需登入、不呼叫真實 LLM、不使用 pytest：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_step1.py -v
```

全專案離線測試：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

測試涵蓋完整句、清單、因果承接、三句以上因果鏈、表格、標題祖先、替換長度映射、unresolved/warnings、錯誤位置／遺漏／重疊、穩定 ID、Schema、CLI、原子寫入競爭與來源不變。現有真實 Step 0 Sample 使用離線人工邊界標註，驗證 26 條正文＋3 條表格、因果鏈合併與追溯；這證明 Pipeline 可忠實接受該邊界，不代表真實 LLM 已產出 29 條。

2026-10-29 最終離線驗證：Step 1 **55/55** 通過；全專案 **132/132** 通過（既有 CodexClient 30、Step 0 47、Step 1 55）。開工前記錄的 26 個既有程式、文件、Schema 及 JSON 檔案之 SHA-256 均未改變。

## Timeout、Retry 與錯誤

- `--timeout` 預設 180 秒，必須為有限正數。它是每次模型嘗試的 timeout，不是整個 CLI 的總時間上限。
- `--max-retries` 預設 0，表示額外重試次數。沿用 CodexClient 預設只針對 timeout 重試；例如 1 表示最多初次加一次額外嘗試。認證、CLI 安裝、檔案、Step 1 邊界或覆蓋錯誤不自動重試。
- `llm_call_count` 與 `llm_retry_count` 記錄實際 Client 嘗試次數。Step 1 不自行再包一層隱藏重試。
- 「LLM 位置越界／文字不符」：整批拒絕，沒有搜尋補正；檢視模型定位品質後用新的目錄重新執行。
- 「未記錄的非空白正文」：模型遺漏內容，整批拒絕，不把遺漏偽裝成完成。
- 「結果已存在」：換新的輸出目錄，不刪除或覆寫歷史。
- `completed_with_warnings`：查看 `warnings`、`unhandled_regions` 及 `step0_provenance`；表示明確記錄了保留事項，並非隱藏失敗。
- `table_pending`：表格保存完整原文和位置，尚未建立條文；不代表欄位已解析完成。
- 硬連結不支援或權限錯誤：使用支援 `os.link` 的本機檔案系統目錄。程式不退回到可能覆寫檔案的發布方式。

## 已知限制與舊版比較

目前沒有舊版正式 Step 1 JSON，因此無法逐欄比較。已在離線標註測試保留舊版已知的 26＋3 拆分形式、三行因果鏈和表格列追溯，並修正同級標題被誤當父章節的問題。新版直接保留現有 Step 0 欄位與 `python_validation` origin，不做舊欄位假設。

語意品質由 LLM 決定。Fake Client 測試不能證明實際模型是否過度合併、判斷正確因果或能穩定給出精確 offset；這些留待手動真實驗收。Python 不推論因果，也不能從 reason 字串證明語意判斷正確。長文件目前採單次完整上下文呼叫，沒有分塊與續傳；可能遇到模型上下文、輸出限制或逾時，此時安全拒絕。

複雜 Markdown 採保守保留策略，可能使可由人工理解的內容先進入未處理區域。`original_mapping=unavailable` 是明確限制，不以猜測位置補齊。輸出會重複保存完整表格脈絡，以確保每條可追溯，JSON 檔案可能較大。未增加新的外部依賴，也未實作 Step 2～8。
