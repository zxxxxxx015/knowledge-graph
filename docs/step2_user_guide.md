# Step 2 使用手冊：知識項提取與知識價值過濾

## 範圍與執行

Step 2 唯一正式輸入是已成功完成且通過完整性驗證的 Step 1 JSON。只從既有條文正文提取核心概念，審查知識價值，並在單一文件內去重。不重跑指涉還原、不重新切割條文、不讀取來源文件或 Step 0 JSON；來源文件與上游 JSON 全部保持原樣。Step 3～8 未實作。

在專案根目錄執行以下命令，可依序執行 Step 0 → Step 1 → Step 2。此命令**會呼叫真實 LLM**，本輪施工並未執行：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_pipeline ".\source\sample_technical_incident.md" --until 2
```

預設仍執行到 Step 1；`--until 0`、`--until 1` 維持原行為。每步預設 `--timeout 360 --max-retries 0`，timeout 分別套用到每次嘗試，並非整個流程的總時間。每次原子建立新的執行目錄，成功結果依序存於 `step0/`、`step1/`、`step2/`；終端機顯示各步狀態、輸出路徑、條文數及 Step 2 審查／提取統計。

獨立執行時，把 `--input` 改為實際存在且通過現行 Step 1 驗證器的 JSON，並使用新的輸出目錄：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_step2 --input ".\outputs\my-run\step1\sample_technical_incident.step1.json" --output-dir ".\outputs\my-step2-run"
```

輸入 `sample_technical_incident.step1.json` 對應輸出 `sample_technical_incident.step2.json`。其他合法 Step 1 JSON 檔名會移除 stem 尾端的 `.step1`，再加 `.step2.json`。獨立 CLI 也預設 timeout=360、max-retries=0。

退出碼：0 成功（包含 completed_with_warnings）、1 LLM 或執行狀態失敗、2 參數／輸入／位置／Schema／檔案錯誤、130 使用者中斷。Step 2 失敗不發布正式結果，已成功的 Step 0 與 Step 1 JSON 保留；可能留下空的 Step 2 目錄。沒有條文時不呼叫 LLM，仍發布零筆審查的完整可追溯結果。

## 檔案與介面

- [處理程式](../src/pipeline/step2_extract.py)：`load_step1`、`Step1Input`、`Step2Extractor.process`、`validate_result`、`prepare_output_path`、`save_result`、`run_step2`。
- [Prompt](../prompts/step2.md)、[LLM Schema](../schemas/step2_llm.schema.json)、[Result Schema](../schemas/step2_result.schema.json)。
- [獨立 CLI](../scripts/run_step2.py)、[統一入口](../scripts/run_pipeline.py)。匯入不啟動 LLM。
- [Step 2 離線測試](../tests/test_step2.py)、[統一入口離線測試](../tests/test_run_pipeline.py)。

只沿用既有 `CodexClient.generate_json()`，沒有直接呼叫 OpenAI API，也沒有重寫 Client。重用 Step 1 的完整性驗證、原文映射與 Step 0 追溯函式，未修改已驗收的 Step 0／1 核心、Prompt 或 Schema。Result Schema 維持既有格式，內嵌 Step 1 與含位置的正式審查資料 Schema，使用本地引用，不依賴遠端 Schema；內嵌定義雖沿用名稱 `llm`，本版代表 Python 定位後的儲存格式，不再等同送給模型的片段 Schema。

先前的 CLI 相容性修正保留：呼叫前對完整 v3 LLM Schema 建立深層副本，**只移除傳送副本的 `properties.concept_groups.items.properties.candidate_ids.uniqueItems`**。執行時不修改磁碟或 Extractor 的完整 Schema；Client 用副本驗證後，Python 仍以完整 v3 Schema 驗證原始回應，接著定位並執行既有候選引用、重複群組、衝突與追溯檢查。本次 v3 僅完成離線驗證，真實 LLM 效果仍待驗收。

## 現行證據定位契約 v3

Prompt 標記為 `step2-text-evidence-v3`。同一次 Step 2 LLM 呼叫先逐條提取，再做完整性複查：檢查主詞、受詞、條件指標、結果、定義句、多句因果鏈各環節及表格每個資料儲存格。可提取設備、方法、指標、參數、專業資料類型及獨立術語／符號；不得為增加數量而擷取一般名詞、整段條件、事件、行為或狀態變化，也不得拆壞複合技術名詞。這是提示與人工標註回歸要求，Fake Client 測試不能保證真實模型召回率。

候選提供 `surface_text`，身分依據提供 `statement_id` 與 `text`；兩者都需提供 `evidence_quote`，值可為 `null` 或逐字字串。目標片段在所屬條文中唯一時可用 `null`。若片段重複，引文必須在同一條文中唯一，且目標片段在引文中唯一。Python 使用兩次精確比對取得 Unicode code point 半開區間 `[start,end)`，再驗證候選／身分證據的表格儲存格、processed_text 絕對位置與原文映射。引文即使在片段唯一時提供，也必須通過驗證。零命中、多命中、重疊命中、跨條文取證或儲存格外片段均拒絕整批，不選第一個、不做模糊比對或正規化。每次出現需有 LLM 明確提出的獨立候選；Python 不自動建立同字串的其他 occurrences。

LLM Schema 將 `evidence_quote` 設為必要但可為 `null`，符合目前結構化輸出的全欄位必要契約；正式 Result Schema 將它設為**可選**，使既有 v2 JSON 不必修改仍可驗證。v3 正式結果的 `extraction_decisions`、`identity_evidence`、`occurrences` 及必要的 `pending_candidates` 保留引文及 Python 位置。`step1_snapshot`、來源追溯、review、群組、ID 輸入與原子發布規則維持。群組成員或來源錨點改變會使知識項 ID 改變，舊輸出不可覆寫。

知識價值先獨立於「是否含技術名詞」判斷：技術定義、條件與結果、規則、量測範圍、機制、觀察影響或處置具內容性知識時為 kept，且至少有一個正式概念。純文件主題介紹、章節導覽或沒有新增資訊的摘要可 filtered，即使提及技術名詞；重述其他條文時 reason 應指出對應 statement_id。介紹句若包含其他條文沒有的具體技術事實可 kept，疑義則 needs_review。不能用「本文件」等字樣作機械過濾。

人工標註案例包括 PID 控制邏輯句的目標值／量測值／閥門開度、多句延遲因果鏈的中間概念、資料品質與 stale 狀態標籤及監控畫面的逐一評估、stale 資料定義中的時間戳。`stale` 可作為具名品質狀態標籤；「監控畫面」限於明確的技術操作介面；「時間戳」是可獨立定義的資料欄位概念。「黃色警示」若只描述呈現效果，不自動當成獨立技術概念；「允許門檻」若缺少特定參數身分，保留待審。這些例子寫在 Prompt 與測試資料，不是生產程式的硬編碼詞表。

## 歷史證據定位契約 v2（供既有輸出對照）

本版為 `step2-text-evidence-v2`，在 Prompt 明確標記，正式結果的 `status.prompt_sha256` 保留使用的 Prompt 身分。這是契約版本調整，沒有在舊索引模式加上搜尋 fallback。

| 資料 | 舊 LLM 回應 | v2 LLM 回應 | Python 定位後的正式儲存 |
| --- | --- | --- | --- |
| candidate | candidate_id、surface_text、start、end、unit_kind、resolution、reason | 移除 start／end，其他欄位保留 | 原有欄位全部保留，start／end 由 Python 建立 |
| identity_evidence | statement_id、start、end、text | 只提供 statement_id、text | 保留 statement_id、text，並由 Python 建立 start／end |

完整 v2 Schema 禁止模型回傳 start／end；即使位置剛好正確，也不能繞過新契約。Python 只在指定 statement_id 所屬條文的原始 `text` 中比對片段，不搜尋全文、標題、表頭或其他條文。

使用完全一致的字串比對，計入重疊命中（例如 `aaaa` 中的 `aa` 有多個命中）。必須恰好一個命中；零命中、多命中一律拒絕整批，不取第一筆、不推測多筆 occurrences、不默默略過候選，也不把失敗自動改為 filtered／needs_review。此規則同樣適用 pending 候選及 identity_evidence。

禁止模糊比對、大小寫轉換、Unicode 正規化、移除空白、改寫換行或補字；也不能為了唯一命中而將核心概念擴充為行為或條件敘述。跨條文重複概念仍可各自唯一定位並共用知識項 ID；同一條文內相同片段重複時，本版會明確失敗，需另行釐清證據，不能用出現序號猜測。

表格候選及 identity_evidence 均須完全落在單一合法資料儲存格內。先檢查整條文的唯一命中，再檢查儲存格邊界；不藉由丟棄其他命中來挑選其中一筆。表格證據不能使用含管線的整列作為片段；可引用資料儲存格的原始文字。表頭仍僅供語意脈絡使用。

精確命中只證明文字存在，不證明概念原子性或語意相同。原有 kept／filtered／needs_review、同義消歧、每個成員條文的身分依據及群組引用／衝突規則均保留；沒有有效身分證據時不得強迫合併。

## JSON 結構

| 頂層欄位 | 內容 |
| --- | --- |
| `input` | Step 1 JSON 的絕對路徑、原始 bytes SHA-256 與 bytes 大小。 |
| `source` | 上游來源識別原樣保留，包含原始來源 SHA-256。 |
| `step1_snapshot` | 完整且未修改的 Step 1 資料，包含 original_text、processed_text、step0_provenance、structure、knowledge_statements、unhandled_regions、warnings、status 及 input。 |
| `extraction_decisions` | 完整保留模型審查／候選／群組及證據欄位，並加入 Python 建立的 start／end，維持原有儲存格式。模型判斷為 metadata，不是補充的來源事實。 |
| `statement_reviews` | 每個既有 statement_id 恰好一筆：decision、basis、reason、knowledge_item_ids、pending_candidates。條文原文及來源可沿 statement_id 查詢完整快照。 |
| `knowledge_items` | 去重後已確認概念：knowledge_item_id、canonical_label、disambiguation、merge_reason、identity_evidence、identity_input、occurrence_ids、source_ref。 |
| `occurrences` | 每次出現各一筆：occurrence_id、knowledge_item_id、statement_id、source_ref、surface_text、statement_local_range、processed_range、original_mapping、step0_trace、table_cell_ref。 |
| `warnings` | 上游問題保留、待審或原文不能精確映射等 Step 2 警告。上游警告詳情保存在快照中。 |
| `status` | execution_state、Prompt ID／SHA-256、LLM 嘗試／重試數、reviewed_statement_count、kept_count、filtered_count、needs_review_count、knowledge_item_count、occurrence_count。 |

所有位置均採 Python Unicode code point 半開區間 `[start,end)`；BOM、CR、LF、空白與 emoji 都依原始字元計數。Python 由片段唯一定位得到 statement-local range，再加上條文起點建立 processed_text 絕對位置，逐字驗證兩者。原始回應不含索引；保存的候選與 identity_evidence 索引全由 Python 建立。重驗結果時會重新執行同一套精確定位，核對保存位置，不能只相信既有數字。

`pending_candidates` 保存 candidate_id（暫時參照）、surface_text、Python 建立的 start／end、unit_kind、resolution=pending、reason，以及與 occurrence 相同的位置、原文映射及追溯欄位；沒有正式概念或 occurrence ID。即使條文為 kept，語意未確認但能唯一定位的候選仍留在 pending；無法定位的候選不會被當成定位成功的 pending 紀錄。

## 概念與保守審查

知識項是需要脈絡才能理解的最小核心概念，原子性不是最短字串。例如「控制迴圈延遲」保留為完整技術概念；「控制迴圈延遲增加」是狀態變化。「CPU 使用率高於 90% 持續 30 秒」是條件，只提取其中的「CPU 使用率」。行為、條件、狀態變化及因果敘述完整留在既有條文中，不直接作為正式概念。

| decision | basis | 判準與正式輸出 |
| --- | --- | --- |
| kept | knowledge_bearing | 有明確知識價值，至少有一個經位置驗證、unit_kind=concept 且 resolution=confirmed 的候選，並完整歸入概念群組。 |
| filtered | no_knowledge_value | 明確無獨立知識價值，例如純介紹、導覽或無新增內容的摘要；即使包含技術名詞仍可使用。必須有具體理由。只保留 review、原條文快照及必要 pending 紀錄，不建立正式概念。 |
| needs_review | uncertain | 價值、概念身分、指涉或同義關係不明；保留理由與可定位的 pending 候選，不建立正式概念。 |

不能因難以理解、不完整或不確定而過濾。kept 缺少已確認概念、filtered／needs_review 卻帶 confirmed 候選，或非 concept 類型被確認，一律拒絕發布。語意價值與 unit_kind 由 LLM 判斷；Python 驗證其契約及可追溯性，不能單靠字元驗證證明語意判斷正確。

## 去重與 ID

模型使用本次回應內全域唯一的 candidate_id 作暫時參照。每個 confirmed 候選恰好屬於一個 concept_groups 群組，每個 pending 候選不得加入群組。群組需提供 canonical_label、具體 disambiguation、單獨保留或合併理由，以及每個成員條文內可唯一精確定位的 identity_evidence。canonical_label 必須等於某個成員實際出現的文字，不可補上外部全名。

同義必須有上下文依據。同名異義使用不同消歧與群組；關係不明保留 pending，不靠文字相同或相似自動合併。Python 拒絕未知候選、群組內重複、跨群重用、遺漏 confirmed 候選，以及完全相同 canonical_label＋disambiguation 的重複身分群組。語意證據是否足以支持同義，仍須透過真實 LLM 驗收與必要人工審查。

正式 ID 由 Python 計算。`identity_input` 完整記錄以下內容：

```json
{
  "version": "step2-concept-anchors-v1",
  "source_sha256": "原始來源 SHA-256",
  "anchors": [
    {"statement_id": "既有條文 ID", "start": 0, "end": 7}
  ]
}
```

anchors 包含此群組全部候選的條文局部範圍，依 statement_id、start、end 排序。以 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)` 序列化，UTF-8 編碼後計算 SHA-256，前加 `ki_`。

occurrence ID 以包含 knowledge_item_id、statement_id、start、end 的物件採同一序列化方式計算 SHA-256，前加 `occ_`。一群多次出現共用 knowledge_item_id，但各有不同 occurrence_id。同名異義因成員錨點不同而有不同 ID。

這是**以來源錨點識別已確認概念群組**，不把模型任意取的概念代號或說明文字當作身分。回傳順序、candidate_id 改名、理由或 canonical_label 的選用改變，不影響同一已確認成員集合的 ID。來源內容或群組成員集合改變，ID 也會改變；尚未提供增量更新身分延續或跨文件全域實體解析。

## 表格與來源映射

kind=table 的模型輸入保留 table_context 的表頭與對應儲存格，用於理解欄位語意。候選必須完全落在其中一個資料儲存格，不能跨越 Markdown 管線或把表頭當出現位置。table_cell_ref 保存 table_id、row_index、column_index、cell_processed_range、header_text、header_range。行列索引由 0 開始。

每個 occurrence 的原文映射由 Step 0 replacements 精確計算。完整替換字串可映射至原始指涉字串，兩者長度或字面可以不同；若候選邊界位於替換字串內部，original_mapping.status=unavailable，source_range／text 為 null，並提供理由，絕不猜測。

step0_trace 索引指向 `step1_snapshot.step0_provenance` 的 replacements、unresolved、warnings。表格 occurrence 同時追溯出現文字及對應表頭儲存格；未定位的上游問題保留於 unlocated 索引。條文整體的既有 step0_trace 仍原樣保存在快照內。heading_context 只作理解與消歧，不會成為新的來源出現位置。

## 發布與失敗

先檢查輸入及輸出可寫性，才呼叫 LLM。輸出不能等於 Step 1 JSON、Step 0 JSON 或原始來源。結果先通過 Result Schema，再由 extraction_decisions 重建並核對所有正式連結、ID、位置、映射、警告與統計。發布前重新讀取 Step 1 JSON，核對輸入原始 bytes SHA-256 及完整快照，避免執行中輸入變動。

採同目錄暫存檔、flush／fsync 與 `os.link` 原子發布，禁止覆寫既有檔案（含競爭寫入及 symlink）。失敗清除暫存檔。上游成功結果不刪除、不覆寫；統一入口會回報保留路徑。

`cli_execution` 失敗訊息會自動附上 Client 最後一次 attempt 的整數 CLI 退出碼，例如 `CLI returncode=17`，同時保存在 `Step2LLMError.returncode`；無法取得合法整數時顯示 `unknown`、屬性為 `None`。獨立 CLI 與統一入口都會顯示此訊息，但自身退出碼仍為 1。其他錯誤分類的訊息維持原行為。不新增 stderr 擷取或診斷開關，不輸出 Client 原始錯誤訊息、Prompt、文件內容、憑證或環境變數。

定位契約錯誤使用 `Step2EvidenceError`，保留固定的 `code` 與 `role`（candidate、identity_evidence、review 或 response），CLI 顯示分類但不回印證據文字、模型任意提供的 ID 或來源內文：

| code | 意義 |
| --- | --- |
| llm_schema_invalid | 原始回應不符合完整 v2 Schema，包括空白片段、舊版索引欄位或重複 candidate_ids。 |
| evidence_empty | 定位器收到空白或非字串片段；正常模型回應通常先被 Schema 阻擋。 |
| evidence_not_found | 在指定條文中零命中。 |
| evidence_ambiguous | 指定條文中多個命中，包含重疊命中。 |
| table_cell_mismatch | 唯一命中未完整位於一個合法資料儲存格。 |
| statement_reference_invalid | 模型引用不存在的條文。 |
| identity_reference_invalid | 身分證據引用群組成員以外的條文。 |
| derived_range_mismatch | 正式儲存索引與 Python 重新定位的結果不一致。 |
| processed_range_mismatch | 條文局部位置換算後，與 processed_text 的切片不一致。 |

僅使用 2026/10/30 已完成的 Step 1 JSON 驗收時，可執行下列一行；會呼叫真實 Step 2 LLM，零重試、使用新的輸出目錄，不重新執行 Step 0／1。本輪離線修正沒有執行此命令：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_step2 --input ".\outputs\pipeline\20261030-111144-ts3bsf0i\step1\sample_technical_incident.step1.json" --output-dir (Join-Path ".\outputs\step2" ("verify-1030-" + (Get-Date -Format "yyyyMMdd-HHmmss-fffffff"))) --timeout 360 --max-retries 0
```

文件內容是不可信輸入。Prompt 明確禁止文件內指令覆蓋任務、要求工具或外部資料存取。模型只收到處理後條文與其標題／表格脈絡，不收到原始來源路徑或巢狀 original_mapping 作為替代提取來源。

## 離線驗證與待驗風險

所有測試使用 unittest、Fake／Mock Client，並封鎖真實 Client 子程序：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_step2 -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

測試涵蓋多概念、原子性、非概念隔離、跨條文去重、同條文及重疊命中歧義、零命中、禁止近似或正規化、明示同義與同名異義、待審、過濾、review 完整性、表格儲存格及 Unicode／CRLF／Markdown 精確位置、Step 0 長度差異映射／未解析紀錄、identity_evidence 定位及篡改重驗、非法引用、幻覺文字、錯誤 Schema／JSON／LLM 失敗、群組衝突、ID 確定性、防覆寫、原子競爭寫入及真實 Python 流程搭配 Fake Client 的串接。

離線測試驗證契約及工程安全，不能證明真實 LLM 的概念召回率、原子性、保守過濾或同義判斷品質。尚待真實驗收精確片段、重複片段限制、表格證據選擇、提示注入防禦及同名異義；模型即使提供逐字存在的證據，也可能作出錯誤語意判斷。單份文件目前一次呼叫，不提供長文件分批／增量去重，受模型上下文、timeout 及既有 Client 輸出大小限制。失敗會拒絕正式發布。

正式 Result Schema、頂層欄位與 ID 演算法維持不變；新舊差異在 LLM 輸入／回應契約及數值位置的產生責任。歷史 JSON 不修改、不自動轉換。舊結果若含同條文重複片段或跨儲存格證據，雖仍符合既有 JSON 形狀，也不保證通過 v2 的唯一定位重驗。Step 2 已無要求 LLM 計算索引的候選或 identity_evidence 欄位；Step 0／1 的既有契約不在本輪修改範圍內。

## 與施工命令單的對應

必要欄位均已實作。額外保存 extraction_decisions、basis、identity_input 與 identity_evidence，供完整性重建及人工稽核；original_text 等上游資料集中放在完整 step1_snapshot，避免建立會與上游分歧的副本。既有統一入口測試中，無效 `--until 2` 改為無效 `--until 3`，並新增 Step 2 測試，維持預設執行到 Step 1。沒有修改歷史輸出、來源文件或既有核心處理邏輯。
