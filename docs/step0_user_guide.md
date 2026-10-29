# Step 0：資料前處理（指涉還原）操作手冊

## 目的與範圍

Step 0 根據來源上下文，將能唯一辨識的代名詞、指示性簡稱或省略性名稱還原成原文明載的完整名稱，降低後續拆分與提取的指涉歧義。LLM 只提出候選與證據；所有變動都由 Python 驗證後按原文切片套用。

不進行摘要、自由改寫、知識項提取或關係建立，也不建立知識條文 ID 或知識項 ID。來源雜湊、Prompt 識別與字元位置只用於技術追蹤。

本次沒有修改原始來源、既有歷史 JSON、[共用 LLM 模組](../src/llm/codex_client.py) 或 [requirements.txt](../requirements.txt)，沒有增加依賴。開發驗證使用專案現有 Python 3.12 與 jsonschema，全部採 Fake Client／Mock，不執行真實 LLM 呼叫。

## 設計依據與文件差異

已閱讀現有 [前置理解](../文獻/前置理解.md)、[詞彙定義](../文獻/知識圖譜專案的詞彙定義.md)、[範例文件](../source/sample_technical_incident.md) 與 [說明文件 PDF](../文獻/知識圖譜說明文件.pdf) 的可抽取文字；PDF 未作為本輪讀取器支援格式。

| 現有資料的差異 | 本輪處理 |
| --- | --- |
| 詞彙定義的知識包段落使用「觀測」，前置理解指定正式名稱為「觀察」 | Step 0 不做分類，也不更名術語；原始文件中的文字照存 |
| PDF 使用較早的階段 1.1～5.3 分期，六大知識包的呈現順序也與前置理解不同 | 依本輪施工指令與兩份 Markdown 定義處理 Step 0；不沿用 PDF 分期實作其他步驟 |
| 前置理解的通用溯源要求包含目前／前一步 ID，本輪明確禁止 Step 0 建立知識 ID | 以來源 SHA-256、完整原文及 offsets 建立技術溯源，不建立知識實體 ID |
| PDF 有原子切句、過濾、補充、建圖與問答流程 | 均屬本輪範圍之外，沒有實作 |

上述差異沒有透過修改來源文件或更動專案核心定義解決。

## 檔案分工與執行流程

- [source_loader.py](../src/ingestion/source_loader.py)：唯讀載入來源、計算 SHA-256，與 LLM／Step 0 邏輯分離。
- [step0.md](../prompts/step0.md)：唯一正式 Prompt；沒有版本切換機制。
- [step0_llm.schema.json](../schemas/step0_llm.schema.json)：LLM 候選與 unresolved 的契約。
- [step0_safety.py](../src/pipeline/step0_safety.py)：位置、重疊、受保護內容、邊界及重建驗證。
- [step0_preprocess.py](../src/pipeline/step0_preprocess.py)：呼叫既有 CodexClient、建立完整結果、驗證與安全保存。
- [step0_result.schema.json](../schemas/step0_result.schema.json)：正式 Step 0 結果契約。
- [run_step0.py](../scripts/run_step0.py)：獨立 Python CLI。
- [test_step0.py](../tests/test_step0.py)：離線測試。

流程如下：

1. 以 bytes 讀取一次來源，嚴格 UTF-8 解碼；保留 BOM、CRLF／LF、標點與所有空白，不做 Unicode 正規化。
2. 計算原始 bytes 的 SHA-256、大小與解碼後 Python 字元數。確認輸出路徑沒有既有結果並預先檢查可寫權限。
3. 載入唯一正式 Prompt 與兩份本機 Schema。把完整原文及每行絕對範圍放進來源資料區塊，不截斷文章。
4. 只呼叫一次既有 `generate_json()` 方法；額外嘗試由共用 client 的設定處理，Step 0 不另設重試迴圈。
5. 再次驗證候選 JSON Schema，逐筆檢查原文、證據、上下界、重疊、名稱規則、受保護內容及中英文邊界。
6. 將合法候選依原文位置排序，以未變動片段加上替換名稱組合全文，計算每筆處理後位置。
7. 比較 Markdown／換行結構；若不一致，整批候選回復原文並寫入拒絕紀錄。
8. 驗證 Result Schema、完整重建、來源雜湊、位置與統計，再發布獨立 JSON。

空文件、只有 BOM 或空白的來源直接保存完整文字與零呼叫紀錄，不呼叫 LLM。

## 指涉還原與安全原則

所有名稱必須有原文依據。候選需宣告 `context=specific_reference`、`is_unique=true`，且完整名稱須在指定證據內逐字出現，至少有一個出現位置在待替換處之外。Python 不使用模型常識補名稱，也不拼湊原文未出現的名稱。

候選位置與證據位置必須完全吻合原文字串。不搜尋第二個相似位置，不模糊比對、不猜測 offset，也不把 UTF-16／byte offset 自動換算成猜測位置。負數、越界、倒置、空範圍與文字不符都拒絕。所有互相重疊的候選（包括重複候選）全部拒絕；同一處同時被標為 unresolved 與替換候選時也拒絕替換。

通用定義、定義／術語章節、文件自稱、Markdown 標題與結構符號均受保護。程式碼區塊、inline code、縮排程式碼、URL、路徑、常見檔名、含底線識別字、連結／參照標籤、HTML、數學式與 front matter 採保守保護；表格儲存格中的自然語言可替換，但管線分隔符、分隔列及原有欄位結構不變。

Python 僅接受短名稱或保守代名詞集合。省略名稱必須逐字包含在完整名稱中；指示性簡稱只移除可辨識的指示前綴並保留名詞。含數字的原文候選、常見動作／條件／情態敘述、標點、換行或非名稱字元不允許替換。這些是通用的保守規則，沒有依 APC 範例建立特定名稱替換表。

若替換會新產生漢字與英文字母直接相連，或黏合英文單字／識別字，拒絕套用。不自行新增空格。完整名稱中的內部空格只能逐字來自證據。這可能降低替換數量，是本輪刻意保留的安全策略。

## 輸入與輸出

第一版接受 UTF-8 `.md` 與 `.txt`（副檔名不分大小寫）。讀取器不支援 PDF、Word、Big5 或 UTF-16；有需要時請另存轉換副本，保留原始來源。

輸出為 `<來源檔名主體>.step0.json`。例如範例文件會得到 `sample_technical_incident.step0.json`。不同來源若同名，請使用不同輸出目錄，避免路徑衝突。

輸出目錄可自動建立。已有結果時在 LLM 呼叫前停止；最後發布時也再次防止競爭條件覆寫。先將完整 UTF-8 JSON 寫入同目錄暫存檔、flush／fsync，再以 `os.link` 發布新檔名，最後清理暫存檔，不使用 `os.replace`。這要求輸出檔案系統支援 hard link；Windows NTFS 適用。不支援的檔案系統會安全失敗，不退回覆寫方式。

輸出完整包含來源內容，因此可能包含來源本身的敏感資料；請由使用者選擇適當的輸出位置。這裡不會將登入憑證、原始 CLI stderr 或無關檔案附加到結果。

## JSON 主要欄位

| 欄位 | 內容 |
| --- | --- |
| `source` | `path` 絕對來源路徑、`document_type`、原始 bytes 的 `sha256`、`size_bytes`、原文 `char_count`、`encoding` |
| `original_text` | 完整原文，不截斷、不正規化換行、不移除 BOM |
| `processed_text` | 由 Python 套用合法替換後的完整全文；未替換部分逐字一致 |
| `replacements` | 只保存成功替換，依來源位置排序 |
| `unresolved` | LLM 無法確認的指涉、Python 拒絕候選及 LLM 失敗保留紀錄 |
| `warnings` | 位置錯誤、證據不符、重疊、不安全邊界、結構異常等技術警告 |
| `status` | 執行狀態、正式 Prompt 路徑與 SHA-256、呼叫／重試數及各類統計 |

所有字元範圍以 `{ "start": 整數, "end": 整數 }` 表示，採 Python Unicode code point 的 `[start,end)`；不是 bytes 或 UTF-16 code units。CR、LF 各算一字元，emoji 按 Python 字串實際長度計算。`source_range`、`evidence_range` 永遠指向 original_text；`processed_range` 指向 processed_text。

每筆成功替換包含：

- `original_text`、`replacement_text`。
- `source_range`、Python 計算的 `processed_range`。
- 逐字原文 `evidence_text`、`evidence_range`。
- `reference_type` 與 LLM 的 `reason`。

每筆 unresolved 包含：

- 已驗證的 `original_text`、`source_range`。
- 原始回報的 `reported_text`、`reported_range`。
- 被拒絕時的 `proposed_replacement`、`reason_code`、`reason`。
- `origin`：`llm`、`python_validation` 或 `llm_failure`。
- `candidate_index`：被拒絕候選的零起算陣列索引，其餘為 null。它不是知識實體 ID。

若 LLM 回報的位置或文字錯誤，`source_range=null`、已驗證的 `original_text=""`，只在 `reported_*` 保留原始宣稱。這避免把錯誤 offset 宣稱為可追溯的原文位置。LLM 失敗時，以完整文件範圍建立一筆 `llm_failure`，不假裝已逐處辨識指涉。

每筆 warning 包含 `code`、`message`、已驗證的 `source_range`（或 null）與 `candidate_index`（或 null）。一筆被拒絕候選會保留一筆 unresolved 與一筆 warning；LLM 主動標記的正常 unresolved 不一定產生 warning。

`status.execution_state`：

| 狀態 | 意義 |
| --- | --- |
| `completed` | 處理完成，沒有 unresolved 或 warnings |
| `completed_with_warnings` | 處理完成，但仍有 unresolved 或 warnings；可能 warning_count=0 而 unresolved_count>0 |
| `llm_failed` | LLM 執行失敗或候選回應未通過 Schema，整篇保持原文 |

其他 status 欄位為 `prompt_id`、`prompt_sha256`、`llm_call_count`、`llm_retry_count`、`candidate_count`、`applied_count`、`rejected_count`、`unresolved_count`、`warning_count`、`llm_error_kind`。`candidate_count = applied_count + rejected_count`，其餘數量也會與實際陣列交叉驗證。

以下是以 Fake Client 實際產生並通過驗證的小型結果（來源路徑為範例）：

```json
{
  "source": {
    "path": "C:/example/input.md",
    "document_type": "md",
    "sha256": "dd3479b2c8b170e4d0f5c8cc4f744fdf565b94113c4217e87d7c66886146206a",
    "size_bytes": 38,
    "char_count": 14,
    "encoding": "utf-8"
  },
  "original_text": "北區控制器。\n控制器運作。\n",
  "processed_text": "北區控制器。\n北區控制器運作。\n",
  "replacements": [
    {
      "original_text": "控制器",
      "replacement_text": "北區控制器",
      "source_range": {"start": 7, "end": 10},
      "processed_range": {"start": 7, "end": 12},
      "evidence_text": "北區控制器。",
      "evidence_range": {"start": 0, "end": 6},
      "reference_type": "elliptical_name",
      "reason": "上下文明確指向證據中的完整名稱。"
    }
  ],
  "unresolved": [],
  "warnings": [],
  "status": {
    "execution_state": "completed",
    "prompt_id": "prompts/step0.md",
    "prompt_sha256": "516eac94863197fa76ec2ba80f6ab298b37447bc26a9511b77844595acb927ea",
    "llm_call_count": 1,
    "llm_retry_count": 0,
    "candidate_count": 1,
    "applied_count": 1,
    "rejected_count": 0,
    "unresolved_count": 0,
    "warning_count": 0,
    "llm_error_kind": null
  }
}
```

## 完整性驗證

`validate_result()` 除 Result Schema 外，還會重新計算原文的 SHA-256、bytes 大小與字元數；驗證每筆替換與證據的原文字串、非重疊排序與處理後位置；重新執行名稱／結構保護規則，並比對統計。

`reconstruct_processed_text(original_text, replacements)` 使用原文未變動切片及替換名稱重建全文，再與 processed_text 做完整字串相等比較。另比對換行序列、每行前綴與 Markdown 標記。任何未記錄的文字刪減、標點變動、空白變動或擅自摘要都會驗證失敗，阻止發布結果。

可在專案根目錄用 Python 讀取並再次檢查既有結果（不會呼叫 LLM）：

```python
import json
from pathlib import Path
from src.pipeline.step0_preprocess import validate_result

result = json.loads(Path("output/step0/sample_technical_incident.step0.json").read_bytes().decode("utf-8"))
validate_result(result)
```

後續 Step 1 可先檢查 status，再直接讀取 processed_text，並沿用 source、original_text 與 replacements 的技術溯源。本輪沒有實作 Step 1。

## 正式執行方式（使用者自行執行，會呼叫真實 LLM）

在專案根目錄，使用已安裝依賴的既有虛擬環境：

```powershell
.\.venv\Scripts\python.exe -B -m scripts.run_step0 --input '.\source\sample_technical_incident.md' --output-dir '.\output\step0' --timeout 120 --max-retries 0
```

以下完整路徑指令可從任意 PowerShell 工作目錄直接複製執行：

```powershell
& 'C:\Users\User\Desktop\知識圖譜\.venv\Scripts\python.exe' -B 'C:\Users\User\Desktop\知識圖譜\scripts\run_step0.py' --input 'C:\Users\User\Desktop\知識圖譜\source\sample_technical_incident.md' --output-dir 'C:\Users\User\Desktop\知識圖譜\output\step0' --timeout 120 --max-retries 0
```

CLI 支援 `--input`、`--output-dir`、`--timeout`、`--max-retries`；前兩項必填。也可執行 `--help`，這不呼叫 LLM。不需要啟用虛擬環境的 ps1 腳本。

完成後 stdout 會輸出 JSON 摘要，包含 output_path 與 status；完整結果在輸出檔，不在終端重印來源全文。退出碼：`0` 表示處理完成（可能仍有 unresolved／warnings）、`1` 表示 LLM 失敗且已保存完整原文結果、`2` 表示參數／來源／輸出／驗證錯誤、`130` 表示中斷。不能只看退出碼 0 判斷所有指涉都已還原，請檢查統計與拒絕原因。

## Timeout 與 Retry

預設 timeout=120 秒、max-retries=0。max-retries 是初次嘗試之外的額外次數；設定 2 時至多嘗試 3 次。沿用共用 CodexClient 的預設策略：只在明確提高上限後對 timeout 重試，不對 CLI 失敗、JSON 錯誤或 Schema 不符自行重試。

timeout 是每次 CLI 嘗試的時間，不是整個 Step 0 的總時間；另有程序樹終止與檔案處理時間。每次重試及逾時前的請求都可能消耗額度。`llm_call_count` 與 `llm_retry_count` 直接取自共用 client 的執行紀錄，是 CLI 啟動嘗試數，不是 token 數、內部 HTTP 重試數或精確額度用量。Step 0 不另啟動第二輪模型修補候選。

## 離線測試

在專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_step0 -v
```

全專案測試：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

本次 Step 0 **47 項通過、0 項失敗**；全專案 **77 項通過、0 項失敗**（包含既有共用 client 的 30 項）。所有 Step 0 測試使用 Fake Client，且封鎖真正的 subprocess；案例檔與測試輸出位於 TemporaryDirectory，測試後清理。對現有範例的測試只做唯讀讀取與 Fake Client 處理，沒有覆寫來源或發布正式結果。

覆蓋明確／模糊指涉、通用定義、文件自稱、offset、證據、越界與重疊、中英文邊界、Markdown／程式碼／URL／路徑、數值與術語、偏移計算、SHA-256、完整重建、雙 Schema、LLM 失敗與逾時、CLI 參數／權限、輸出不覆寫及競爭條件。

## 常見錯誤與限制

| 情況 | 處理方式 |
| --- | --- |
| 已有同名 Step 0 JSON | 改用新的 output-dir，程式不會覆寫或刪除既有結果 |
| `invalid_source_range`／`source_text_mismatch` | 原文保留；檢查原始候選位置與 reported_range，不手動猜測移位後再當成正式結果 |
| `invalid_evidence_range`／`evidence_text_mismatch`／`unsupported_replacement` | 原文證據不足或不符，拒絕替換 |
| `overlapping_candidates` | 所有互相重疊候選均保留原文 |
| `unsafe_boundary` | 保留原文，不補空格 |
| `generic_definition`／`document_self_reference`／`protected_content` | 屬保護範圍，拒絕替換是預期行為 |
| `invalid_llm_response` | 整個模型回應不符合候選 Schema，保存完整原文與 llm_failed 狀態 |
| timeout／CLI 執行失敗 | 查看 llm_error_kind 與共用 client 手冊；失敗結果不會部分套用候選 |
| 來源不是 UTF-8 或副檔名不支援 | 另建轉換副本，保留原始來源後再執行 |
| 輸出權限或 hard link 不支援 | 使用可寫的 NTFS 目錄，不繞過防覆寫策略 |
| `ModuleNotFoundError: jsonschema` | 確認使用專案已有依賴的虛擬環境；本輪不另建環境或安裝套件 |

Python 能確保字元、證據與結構安全，無法獨立證明語意上的唯一指涉。這仍由 LLM 依上下文判斷，真實驗收需查看成功替換的證據；模型也可能漏掉指涉。宣告 unique=true 不是數學上的語意正確性證明。

Markdown 保護採保守掃描器，並非完整 CommonMark 語法解析器；特殊巢狀語法可能擴大保護範圍。短名稱限制、數字／動作詞保護與邊界規則也可能拒絕實際合理的名稱。第一版不分塊、不截斷大型來源；超過模型 context 或共用 client 回應大小限制時安全失敗，仍保存完整原文。

輸出發布依賴 hard link；同時啟動多個程序雖然不會覆寫彼此的結果，但仍可能各自發出 LLM 請求，請勿重複啟動相同任務。主機斷電或 Python 被強制終止時，可能留下尚未發布的 `.step0-*.tmp`；沒有自動清除他人暫存檔或歷史結果的機制。

本輪已完成程式與離線測試；**尚未進行真實 Step 0 LLM 驗收**。沒有建立 Step 1～8、Neo4j、RAG 或 GUI。
